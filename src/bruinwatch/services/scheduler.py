"""Tiered, adaptive polling.

The old bot ran a single flat 15-second loop that re-fetched every watched class
for every user. This replaces it with four tiers whose cadence matches how fast
the underlying data actually moves:

===================  ==================  ===================================
Tier                 Cadence             Scope
===================  ==================  ===================================
terms                daily               the term dropdown
subject areas        weekly              per active term
course catalog       daily               every subject in every active term
all sections         hourly              full enrollment history sweep
watched sections     adaptive 30s - 15m  only sections someone subscribes to
===================  ==================  ===================================

The hourly full sweep matches hotseat.io's ``cron(25 * * * ? *)``. Only the
watched tier goes faster, and only when a student could actually act on the
result -- during an open enrollment pass.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from collections.abc import Awaitable, Callable

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..db import models as m
from ..db.session import transaction
from ..registrar import RegistrarClient
from ..registrar.types import Course as CourseDTO
from .sync import (
    SyncResult,
    sync_course_sections,
    sync_courses_for_subject,
    sync_subject_areas,
    sync_terms,
    watched_courses,
)

log = structlog.get_logger(__name__)

CAMPUS_TZ = zoneinfo.ZoneInfo("America/Los_Angeles")

# Watched-tier cadences, fastest first.
BURST_INTERVAL = dt.timedelta(seconds=30)
ACTIVE_INTERVAL = dt.timedelta(minutes=2)
IDLE_INTERVAL = dt.timedelta(minutes=15)

WATCHED_JOB_ID = "watched-sections"

#: Campus-daytime window. Outside it, enrollment barely moves.
DAY_START_HOUR = 7
DAY_END_HOUR = 23


def choose_interval(
    *,
    now: dt.datetime,
    in_enrollment_window: bool,
    circuit_open: bool,
    has_watchers: bool,
) -> dt.timedelta:
    """Pick the watched-section polling cadence.

    Pure so the policy can be tested without a scheduler, a clock or a database.
    Order matters: a tripped breaker outranks everything, because the whole
    point is to stop hitting a registrar that is already failing.
    """
    if circuit_open or not has_watchers:
        return IDLE_INTERVAL
    if in_enrollment_window:
        # The only time sub-minute latency helps anyone: a student can act now.
        return BURST_INTERVAL
    if DAY_START_HOUR <= now.hour < DAY_END_HOUR:
        return ACTIVE_INTERVAL
    return IDLE_INTERVAL


class ScraperService:
    """Owns the scheduler, the registrar client and the circuit breaker."""

    def __init__(
        self,
        settings: Settings,
        factory: async_sessionmaker[AsyncSession],
        client: RegistrarClient,
        *,
        on_alert: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.client = client
        self.scheduler = AsyncIOScheduler(timezone=CAMPUS_TZ)
        self._on_alert = on_alert
        self._consecutive_failures = 0
        self._tripped = False
        self._current_interval: dt.timedelta | None = None
        self.last_result = SyncResult()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self.scheduler.add_job(
            self._guarded(self.sync_terms_job),
            CronTrigger(hour=3, minute=5),
            id="terms",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._guarded(self.sync_subject_areas_job),
            CronTrigger(day_of_week="sun", hour=3, minute=20),
            id="subject-areas",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._guarded(self.sync_catalog_job),
            CronTrigger(hour=4, minute=0),
            id="catalog",
            max_instances=1,
            misfire_grace_time=3600,
        )
        self.scheduler.add_job(
            self._guarded(self.sync_all_sections_job),
            CronTrigger(minute=25),
            id="all-sections",
            max_instances=1,
            misfire_grace_time=600,
        )
        self.scheduler.add_job(
            self._guarded(self.poll_watched_job),
            IntervalTrigger(seconds=int(ACTIVE_INTERVAL.total_seconds())),
            id=WATCHED_JOB_ID,
            max_instances=1,
            coalesce=True,
        )
        self._current_interval = ACTIVE_INTERVAL
        self.scheduler.start()
        log.info("scheduler_started", jobs=[j.id for j in self.scheduler.get_jobs()])

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _guarded(self, job: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        """Never let a job exception kill its schedule."""

        async def runner() -> None:
            try:
                await job()
            except Exception:
                log.exception("scheduled_job_failed", job=job.__name__)

        runner.__name__ = job.__name__
        return runner

    # -- jobs -------------------------------------------------------------

    async def sync_terms_job(self) -> None:
        await sync_terms(self.client, self.factory)

    async def sync_subject_areas_job(self) -> None:
        for term_code in await self._active_term_codes():
            await sync_subject_areas(self.client, self.factory, term_code)

    async def sync_catalog_job(self) -> None:
        """Rebuild the course catalog for every active term.

        Subjects are walked sequentially; the client's semaphore already bounds
        concurrency, and a slow full catalog pass is fine on a daily cadence.
        """
        async with transaction(self.factory) as session:
            subject_codes = list(
                (await session.execute(select(m.SubjectArea.code).order_by(m.SubjectArea.code)))
                .scalars()
                .all()
            )

        for term_code in await self._active_term_codes():
            total = 0
            for code in subject_codes:
                total += await sync_courses_for_subject(self.client, self.factory, code, term_code)
            log.info("catalog_synced", term=term_code, courses=total)

    async def sync_all_sections_job(self) -> None:
        """Hourly full sweep: every course in every active term."""
        courses = await self._catalog_courses()
        result = SyncResult()
        for term_code, course in courses:
            result = result + await sync_course_sections(
                self.client, self.factory, course, term_code
            )
        log.info(
            "full_sweep_complete",
            courses=len(courses),
            sections=result.sections_seen,
            history=result.history_rows,
            notifications=result.notifications,
        )

    async def poll_watched_job(self) -> None:
        """The fast tier: only sections somebody is watching."""
        async with self.factory() as session:
            targets = await watched_courses(session)

        if not targets:
            # Nothing to poll is a healthy state, not a failed one -- without
            # this a tripped breaker could never reset once the last
            # subscription went away.
            await self._record_success()
            await self._retune(
                choose_interval(
                    now=dt.datetime.now(CAMPUS_TZ),
                    in_enrollment_window=False,
                    circuit_open=self._tripped,
                    has_watchers=False,
                )
            )
            return

        result = SyncResult()
        requests = 0
        try:
            for term_code, subject_code, number, _registrar_ids in targets:
                course = CourseDTO(subject_area_code=subject_code, number=number)
                requests += 1
                result = result + await sync_course_sections(
                    self.client,
                    self.factory,
                    course,
                    term_code,
                    # The hourly sweep owns history for the full catalog; the
                    # fast tier records it too so watched classes get a denser
                    # series exactly where people care.
                    record_history=True,
                )
        except Exception:
            await self._record_failure()
            raise
        else:
            await self._record_success()

        self.last_result = result
        log.info(
            "watched_poll_complete",
            watched_courses=len(targets),
            requests=requests,
            sections=result.sections_seen,
            notifications=result.notifications,
        )
        await self._retune(await self._desired_interval())

    # -- adaptive cadence -------------------------------------------------

    async def _desired_interval(self) -> dt.timedelta:
        """Pick a polling cadence for the watched tier."""
        now = dt.datetime.now(CAMPUS_TZ)
        return choose_interval(
            now=now,
            in_enrollment_window=await self._in_enrollment_window(now),
            circuit_open=self._tripped,
            has_watchers=True,
        )

    async def _in_enrollment_window(self, now: dt.datetime) -> bool:
        async with self.factory() as session:
            hit = await session.execute(
                select(m.EnrollmentAppointment.id)
                .where(
                    m.EnrollmentAppointment.start_at <= now,
                    m.EnrollmentAppointment.end_at >= now,
                )
                .limit(1)
            )
            return hit.scalar_one_or_none() is not None

    async def _retune(self, interval: dt.timedelta) -> None:
        """Reschedule the watched job if the desired cadence changed."""
        if interval == self._current_interval:
            return
        job = self.scheduler.get_job(WATCHED_JOB_ID)
        if job is None:
            return
        job.reschedule(IntervalTrigger(seconds=int(interval.total_seconds())))
        log.info(
            "watched_interval_changed",
            previous=str(self._current_interval),
            interval=str(interval),
        )
        self._current_interval = interval

    # -- circuit breaker --------------------------------------------------

    async def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if (
            not self._tripped
            and self._consecutive_failures >= self.settings.circuit_breaker_threshold
        ):
            self._tripped = True
            message = (
                f"Registrar scraping has failed {self._consecutive_failures} times in a row. "
                f"Backing off to {IDLE_INTERVAL}."
            )
            log.error("circuit_breaker_tripped", failures=self._consecutive_failures)
            await self._retune(IDLE_INTERVAL)
            if self._on_alert is not None:
                await self._on_alert(message)

    async def _record_success(self) -> None:
        if self._tripped:
            log.info("circuit_breaker_reset")
            if self._on_alert is not None:
                await self._on_alert("Registrar scraping recovered.")
        self._tripped = False
        self._consecutive_failures = 0

    # -- helpers ----------------------------------------------------------

    async def _active_term_codes(self) -> list[str]:
        async with self.factory() as session:
            return list(
                (
                    await session.execute(
                        select(m.Term.code)
                        .where(m.Term.is_active.is_(True))
                        .order_by(m.Term.position)
                    )
                )
                .scalars()
                .all()
            )

    async def _catalog_courses(self) -> list[tuple[str, CourseDTO]]:
        async with self.factory() as session:
            rows = await session.execute(
                select(
                    m.Term.code,
                    m.SubjectArea.code,
                    m.Course.number,
                    m.CourseTerm.section_indices,
                )
                .select_from(m.CourseTerm)
                .join(m.Term, m.Term.id == m.CourseTerm.term_id)
                .join(m.Course, m.Course.id == m.CourseTerm.course_id)
                .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
                .where(m.Term.is_active.is_(True))
            )
            return [
                (
                    term_code,
                    CourseDTO(
                        subject_area_code=subject_code,
                        number=number,
                        section_indices=tuple(indices or ["%"]),
                    ),
                )
                for term_code, subject_code, number, indices in rows
            ]

    # -- introspection (used by /admin) -----------------------------------

    @property
    def poll_interval(self) -> dt.timedelta | None:
        """Current cadence of the watched-section tier."""
        return self._current_interval

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def circuit_open(self) -> bool:
        """True when the breaker has tripped and we are backing off."""
        return self._tripped

    # -- manual triggers (used by /admin) ---------------------------------

    async def run_now(self, job_id: str) -> None:
        jobs = {
            "terms": self.sync_terms_job,
            "subject-areas": self.sync_subject_areas_job,
            "catalog": self.sync_catalog_job,
            "all-sections": self.sync_all_sections_job,
            "watched": self.poll_watched_job,
        }
        if job_id not in jobs:
            raise KeyError(job_id)
        await jobs[job_id]()


async def bootstrap(client: RegistrarClient, factory: async_sessionmaker[AsyncSession]) -> None:
    """First-run seed: terms and subject areas, so commands work immediately.

    Deliberately does *not* pull the full catalog -- that is a long job and the
    daily trigger will pick it up.
    """
    active = await sync_terms(client, factory)
    for term_code in active:
        await sync_subject_areas(client, factory, term_code)


__all__ = [
    "ACTIVE_INTERVAL",
    "BURST_INTERVAL",
    "IDLE_INTERVAL",
    "ScraperService",
    "bootstrap",
    "choose_interval",
]
