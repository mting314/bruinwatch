"""Scrape -> upsert -> diff -> enqueue.

The heart of the bot, and a port of hotseat.io's ``SaveSection``
(lambdas/fetch-sections/storage.go). Two distinct thresholds govern what a
change is worth:

* the **enrollment status** changed (Open -> Full, Full -> Waitlist, ...)
  -> notify every subscriber, because that is an actionable event;
* **any** enrollment number moved -> append a row to ``enrollment_data``,
  because that is a data point for the history chart.

Both happen in one transaction with the section update, so a crash cannot leave
the stored state ahead of the notifications derived from it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from enum import StrEnum

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import models as m
from ..db.session import transaction
from ..registrar import RegistrarClient
from ..registrar.scrapers import (
    FetchFailures,
    fetch_courses_for_subject,
    fetch_sections_for_course,
    fetch_subject_areas,
    fetch_terms,
)
from ..registrar.types import Course as CourseDTO
from ..registrar.types import EnrollmentNumbers, EnrollmentStatus, WaitlistStatus
from ..registrar.types import Section as SectionDTO
from .changes import classify, notification_reason

log = structlog.get_logger(__name__)

#: Terms whose catalog we keep fresh. Everything else is read-only history.
ACTIVE_TERM_LIMIT = 3

#: Widths of the bounded string columns we write scraped text into. Exceeding
#: one raises StringDataRightTruncation, which aborts the whole transaction --
#: and a backfill is hours long, so one unexpected value must not kill it.
#: A survey of real data found "4.0/6.0 Alternate" (17 chars) in a units field
#: sized at 16; the column was widened, and this is the belt to that braces.
_COLUMN_WIDTHS = {
    "section_label": 32,
    "format": 16,
    "units": 32,
    "title": 256,
    "number": 16,
    "registrar_id": 16,
}


def _clip(field: str, value: str) -> str:
    """Trim a scraped string to fit its column, loudly."""
    limit = _COLUMN_WIDTHS[field]
    if len(value) <= limit:
        return value
    log.warning("value_truncated", field=field, limit=limit, value=value)
    return value[:limit]


@dataclasses.dataclass(frozen=True, slots=True)
class SyncResult:
    sections_seen: int = 0
    history_rows: int = 0
    notifications: int = 0

    def __add__(self, other: SyncResult) -> SyncResult:
        return SyncResult(
            self.sections_seen + other.sections_seen,
            self.history_rows + other.history_rows,
            self.notifications + other.notifications,
        )


# --------------------------------------------------------------------------
# Catalog sync
# --------------------------------------------------------------------------


async def sync_terms(
    client: RegistrarClient, factory: async_sessionmaker[AsyncSession]
) -> list[str]:
    """Refresh the term list and mark the most recent few as active.

    Returns the active term codes. The registrar lists terms newest-first, and
    that order is preserved as ``position`` because term codes cannot be sorted
    chronologically themselves -- see :func:`bruinwatch.registrar.parsing.parse_terms`.
    """
    terms = await fetch_terms(client)
    active = {t.code for t in terms[:ACTIVE_TERM_LIMIT]}

    async with transaction(factory) as session:
        for term in terms:
            values = {
                "code": term.code,
                "name": term.name,
                "position": term.position,
                "is_current": term.is_current,
                "is_active": term.code in active,
            }
            await session.execute(
                pg_insert(m.Term)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["code"],
                    set_={k: v for k, v in values.items() if k != "code"},
                )
            )
        # Any term no longer published stops being polled.
        stale = await session.execute(
            select(m.Term).where(m.Term.code.notin_([t.code for t in terms]))
        )
        for row in stale.scalars():
            row.is_active = False

    ordered = [t.code for t in terms if t.code in active]
    log.info(
        "synced_terms",
        total=len(terms),
        active=ordered,
        current=next((t.code for t in terms if t.is_current), None),
    )
    return ordered


async def sync_subject_areas(
    client: RegistrarClient, factory: async_sessionmaker[AsyncSession], term_code: str
) -> int:
    areas = await fetch_subject_areas(client, term_code)
    async with transaction(factory) as session:
        for area in areas:
            await session.execute(
                pg_insert(m.SubjectArea)
                .values(code=area.code, name=area.name)
                .on_conflict_do_update(index_elements=["code"], set_={"name": area.name})
            )
    return len(areas)


async def sync_courses_for_subject(
    client: RegistrarClient,
    factory: async_sessionmaker[AsyncSession],
    subject_area_code: str,
    term_code: str,
    failures: FetchFailures | None = None,
) -> int:
    """Upsert a subject's course list and its offerings for the term."""
    courses = await fetch_courses_for_subject(
        client, subject_area_code, term_code, failures=failures
    )
    if not courses:
        return 0

    async with transaction(factory) as session:
        term_id = await _term_id(session, term_code)
        subject_id = await _subject_area_id(session, subject_area_code)
        if term_id is None or subject_id is None:
            log.warning(
                "skipping_courses_unknown_parent", subject=subject_area_code, term=term_code
            )
            return 0

        for course in courses:
            result = await session.execute(
                pg_insert(m.Course)
                .values(
                    subject_area_id=subject_id,
                    number=_clip("number", course.number),
                    title=_clip("title", course.title),
                )
                .on_conflict_do_update(
                    constraint="uq_course",
                    set_={"title": _clip("title", course.title)},
                )
                .returning(m.Course.id)
            )
            course_id = result.scalar_one()
            await session.execute(
                pg_insert(m.CourseTerm)
                .values(
                    course_id=course_id,
                    term_id=term_id,
                    section_indices=list(course.section_indices),
                )
                .on_conflict_do_update(
                    index_elements=["course_id", "term_id"],
                    set_={"section_indices": list(course.section_indices)},
                )
            )
    return len(courses)


# --------------------------------------------------------------------------
# Section sync + change detection
# --------------------------------------------------------------------------


async def sync_course_sections(
    client: RegistrarClient,
    factory: async_sessionmaker[AsyncSession],
    course: CourseDTO,
    term_code: str,
    *,
    record_history: bool = True,
    failures: FetchFailures | None = None,
) -> SyncResult:
    """Fetch and persist every section of one course.

    The course row is created if we have not catalogued it yet, so ``/search``
    works on a cold start instead of waiting for the nightly catalog job.
    """
    sections = await fetch_sections_for_course(client, course, term_code, failures=failures)
    if not sections:
        return SyncResult()

    result = SyncResult()
    async with transaction(factory) as session:
        term_id = await _term_id(session, term_code)
        if term_id is None:
            log.warning("skipping_sections_unknown_term", course=course.short_title, term=term_code)
            return SyncResult()

        course_id = await _ensure_course(session, course, term_id)
        if course_id is None:
            log.warning("skipping_sections_unknown_subject", course=course.short_title)
            return SyncResult()

        for section in sections:
            result = result + await save_section(
                session,
                section,
                term_id=term_id,
                course_id=course_id,
                record_history=record_history,
            )
    return result


async def save_section(
    session: AsyncSession,
    section: SectionDTO,
    *,
    term_id: int,
    course_id: int,
    record_history: bool = True,
) -> SyncResult:
    """Upsert one section, recording history and queueing notifications.

    Must run inside a transaction. The previous state is read *before* the
    upsert, which is the whole trick -- read it after and every section looks
    unchanged.
    """
    previous = await _previous_numbers(session, section.registrar_id, term_id)

    enrollment = section.enrollment
    upsert = (
        pg_insert(m.Section)
        .values(
            registrar_id=section.registrar_id,
            term_id=term_id,
            course_id=course_id,
            section_label=_clip("section_label", section.section_label),
            format=_clip("format", section.format),
            index=section.index,
            days=list(section.days),
            times=list(section.times),
            locations=list(section.locations),
            instructors=list(section.instructors),
            units=_clip("units", section.units),
            enrollment_status=str(enrollment.enrollment_status),
            enrollment_count=enrollment.enrollment_count,
            enrollment_capacity=enrollment.enrollment_capacity,
            waitlist_status=str(enrollment.waitlist_status),
            waitlist_count=enrollment.waitlist_count,
            waitlist_capacity=enrollment.waitlist_capacity,
            website=section.website,
            final_start=section.final_start,
            final_end=section.final_end,
            summer_session=section.summer_session,
            summer_duration_weeks=section.summer_duration_weeks,
        )
        .on_conflict_do_update(
            constraint="uq_section",
            set_={
                "course_id": course_id,
                "section_label": _clip("section_label", section.section_label),
                "format": _clip("format", section.format),
                "index": section.index,
                "days": list(section.days),
                "times": list(section.times),
                "locations": list(section.locations),
                "instructors": list(section.instructors),
                "units": _clip("units", section.units),
                "enrollment_status": str(enrollment.enrollment_status),
                "enrollment_count": enrollment.enrollment_count,
                "enrollment_capacity": enrollment.enrollment_capacity,
                "waitlist_status": str(enrollment.waitlist_status),
                "waitlist_count": enrollment.waitlist_count,
                "waitlist_capacity": enrollment.waitlist_capacity,
                "updated_at": dt.datetime.now(dt.UTC),
            },
        )
        .returning(m.Section.id)
    )
    section_id = (await session.execute(upsert)).scalar_one()

    decision = classify(previous, enrollment, record_history=record_history)

    history_rows = 0
    if decision.record_history:
        await session.execute(
            pg_insert(m.EnrollmentDatum).values(
                section_id=section_id,
                enrollment_status=str(enrollment.enrollment_status),
                enrollment_count=enrollment.enrollment_count,
                enrollment_capacity=enrollment.enrollment_capacity,
                waitlist_status=str(enrollment.waitlist_status),
                waitlist_count=enrollment.waitlist_count,
                waitlist_capacity=enrollment.waitlist_capacity,
            )
        )
        history_rows = 1

    notifications = 0
    if previous is not None and previous != enrollment:
        notifications = await _enqueue_notifications(session, section_id, previous, enrollment)

    return SyncResult(sections_seen=1, history_rows=history_rows, notifications=notifications)


async def _enqueue_notifications(
    session: AsyncSession,
    section_id: int,
    previous: EnrollmentNumbers,
    current: EnrollmentNumbers,
) -> int:
    """Queue a DM for every subscriber whose notification condition fired.

    Per-subscriber, because a spots-left threshold is set per subscription; a
    status change fires for everyone, a threshold only for whoever set it.
    """
    subscriptions = (
        (
            await session.execute(
                select(m.Subscription).where(
                    m.Subscription.section_id == section_id,
                    m.Subscription.notify.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    if not subscriptions:
        return 0

    queued = 0
    for subscription in subscriptions:
        reason = notification_reason(previous, current, subscription.notify_below_spots)
        if reason is None:
            continue
        await session.execute(
            pg_insert(m.NotificationOutbox).values(
                user_id=subscription.user_id,
                section_id=section_id,
                previous_status=str(previous.enrollment_status),
                new_status=str(current.enrollment_status),
                reason=str(reason),
            )
        )
        queued += 1
    return queued


async def _previous_numbers(
    session: AsyncSession, registrar_id: str, term_id: int
) -> EnrollmentNumbers | None:
    """The stored enrollment state, or None if we have never seen this section."""
    row = (
        await session.execute(
            select(
                m.Section.enrollment_status,
                m.Section.enrollment_count,
                m.Section.enrollment_capacity,
                m.Section.waitlist_status,
                m.Section.waitlist_count,
                m.Section.waitlist_capacity,
            ).where(m.Section.registrar_id == registrar_id, m.Section.term_id == term_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return EnrollmentNumbers(
        enrollment_status=_enum(EnrollmentStatus, row[0], EnrollmentStatus.UNKNOWN),
        enrollment_count=row[1],
        enrollment_capacity=row[2],
        waitlist_status=_enum(WaitlistStatus, row[3], WaitlistStatus.UNKNOWN),
        waitlist_count=row[4],
        waitlist_capacity=row[5],
    )


def _enum[E: StrEnum](enum: type[E], value: str, default: E) -> E:
    """Read a stored status back, tolerating values written by an older build."""
    try:
        return enum(value)
    except ValueError:
        log.warning("unknown_stored_status", enum=enum.__name__, value=value)
        return default


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


async def _term_id(session: AsyncSession, code: str) -> int | None:
    return (
        await session.execute(select(m.Term.id).where(m.Term.code == code))
    ).scalar_one_or_none()


async def _subject_area_id(session: AsyncSession, code: str) -> int | None:
    return (
        await session.execute(select(m.SubjectArea.id).where(m.SubjectArea.code == code))
    ).scalar_one_or_none()


async def _ensure_course(session: AsyncSession, course: CourseDTO, term_id: int) -> int | None:
    """Return the course's id, creating the row (and its offering) if needed.

    Returns ``None`` only when the subject area itself is unknown, which means
    the caller passed something the registrar doesn't recognise.
    """
    existing = await _course_id(session, course.subject_area_code, course.number)
    if existing is not None:
        return existing

    subject_id = await _subject_area_id(session, course.subject_area_code)
    if subject_id is None:
        return None

    course_id: int = (
        await session.execute(
            pg_insert(m.Course)
            .values(
                subject_area_id=subject_id,
                number=course.number,
                title=course.title or course.short_title,
            )
            .on_conflict_do_update(constraint="uq_course", set_={"number": course.number})
            .returning(m.Course.id)
        )
    ).scalar_one()

    await session.execute(
        pg_insert(m.CourseTerm)
        .values(
            course_id=course_id,
            term_id=term_id,
            section_indices=list(course.section_indices),
        )
        .on_conflict_do_nothing(index_elements=["course_id", "term_id"])
    )
    return course_id


async def _course_id(session: AsyncSession, subject_area_code: str, number: str) -> int | None:
    return (
        await session.execute(
            select(m.Course.id)
            .join(m.SubjectArea)
            .where(m.SubjectArea.code == subject_area_code, m.Course.number == number)
        )
    ).scalar_one_or_none()


async def watched_courses(
    session: AsyncSession,
) -> list[tuple[str, str, str, set[str]]]:
    """Courses with at least one watched section.

    Returns ``(term_code, subject_area_code, course_number, registrar_ids)``.
    Grouping by course rather than by section means one HTTP request covers a
    lecture and all of its discussions at once, and -- crucially -- the same
    section watched by 50 people is still exactly one request.
    """
    rows = await session.execute(
        select(
            m.Term.code,
            m.SubjectArea.code,
            m.Course.number,
            m.Section.registrar_id,
        )
        .select_from(m.Subscription)
        .join(m.Section, m.Section.id == m.Subscription.section_id)
        .join(m.Term, m.Term.id == m.Section.term_id)
        .join(m.Course, m.Course.id == m.Section.course_id)
        .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
        .where(m.Term.is_active.is_(True))
        .distinct()
    )

    grouped: dict[tuple[str, str, str], set[str]] = {}
    for term_code, subject_code, number, registrar_id in rows:
        grouped.setdefault((term_code, subject_code, number), set()).add(registrar_id)
    return [(t, s, n, ids) for (t, s, n), ids in grouped.items()]
