"""Queries behind the stats pages.

Two sources, used for different jobs:

* ``sections`` holds the *current* state of every section. Anything that asks
  "how full is it right now" reads from here -- one row per section, indexed,
  cheap.
* ``enrollment_data`` is the append-only history. Anything that asks "how did it
  get there" reads from here, always constrained to a section (or a small set)
  so the ``(section_id, created_at)`` index does the work.

Nothing here renders; it returns plain dataclasses so the numbers can be tested
without a web server.

One honest limitation runs through all of it: we only know what we have
observed. "Time to fill" is measured from the bot's *first observation* of a
section, not from when the registrar opened enrollment, and the UI says so.
Cross-term comparisons need at least two terms of collected history.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from sqlalchemy import Integer, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import models as m

#: Statuses that mean "you cannot simply enrol".
CLOSED_STATUSES = ("Full", "Closed", "Waitlist", "Cancelled")


@dataclasses.dataclass(frozen=True, slots=True)
class Summary:
    """Headline counts for the overview page."""

    terms: int
    subject_areas: int
    courses: int
    sections: int
    observations: int
    watched_sections: int
    first_observation: dt.datetime | None
    last_observation: dt.datetime | None

    @property
    def has_history(self) -> bool:
        return self.observations > 0

    @property
    def days_of_history(self) -> float:
        if self.first_observation is None or self.last_observation is None:
            return 0.0
        return (self.last_observation - self.first_observation).total_seconds() / 86400


@dataclasses.dataclass(frozen=True, slots=True)
class StatusCount:
    status: str
    sections: int


@dataclasses.dataclass(frozen=True, slots=True)
class CourseDemand:
    """How contested a course is, at its most recent observation."""

    subject_area_code: str
    course_number: str
    title: str
    enrolled: int
    capacity: int
    waitlisted: int
    sections: int

    @property
    def label(self) -> str:
        return f"{self.subject_area_code} {self.course_number}"

    @property
    def demand_ratio(self) -> float:
        """Seats wanted per seat available. Above 1.0 means unmet demand."""
        if self.capacity <= 0:
            return 0.0
        return (self.enrolled + self.waitlisted) / self.capacity

    @property
    def fill_ratio(self) -> float:
        if self.capacity <= 0:
            return 0.0
        return min(1.0, self.enrolled / self.capacity)


@dataclasses.dataclass(frozen=True, slots=True)
class SubjectPressure:
    subject_area_code: str
    name: str
    sections: int
    closed_sections: int

    @property
    def closed_share(self) -> float:
        return self.closed_sections / self.sections if self.sections else 0.0


@dataclasses.dataclass(frozen=True, slots=True)
class FillSpeed:
    """How long a section took to close, from when we first saw it."""

    subject_area_code: str
    course_number: str
    section_label: str
    hours_to_full: float
    capacity: int

    @property
    def label(self) -> str:
        return f"{self.subject_area_code} {self.course_number} {self.section_label}"


@dataclasses.dataclass(frozen=True, slots=True)
class Observation:
    at: dt.datetime
    enrolled: int
    capacity: int
    waitlisted: int
    status: str

    @property
    def fill_pct(self) -> float:
        return 100.0 * self.enrolled / self.capacity if self.capacity else 0.0


@dataclasses.dataclass(frozen=True, slots=True)
class SectionSeries:
    """One section's history, ready to plot."""

    section_id: int
    section_label: str
    instructors: tuple[str, ...]
    capacity: int
    status: str
    points: tuple[Observation, ...]

    @property
    def peak_fill_pct(self) -> float:
        return max((p.fill_pct for p in self.points), default=0.0)


@dataclasses.dataclass(frozen=True, slots=True)
class TermPeak:
    """A course's high-water mark in one term, for cross-term comparison."""

    term_code: str
    term_name: str
    position: int
    peak_enrolled: int
    capacity: int
    peak_waitlisted: int

    @property
    def peak_fill_pct(self) -> float:
        return 100.0 * self.peak_enrolled / self.capacity if self.capacity else 0.0


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------


async def summary(session: AsyncSession) -> Summary:
    async def count(model: type) -> int:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())

    bounds = (
        await session.execute(
            select(func.min(m.EnrollmentDatum.created_at), func.max(m.EnrollmentDatum.created_at))
        )
    ).one()

    return Summary(
        terms=await count(m.Term),
        subject_areas=await count(m.SubjectArea),
        courses=await count(m.Course),
        sections=await count(m.Section),
        observations=await count(m.EnrollmentDatum),
        watched_sections=int(
            (
                await session.execute(select(func.count(func.distinct(m.Subscription.section_id))))
            ).scalar_one()
        ),
        first_observation=bounds[0],
        last_observation=bounds[1],
    )


async def status_breakdown(session: AsyncSession, term_code: str) -> list[StatusCount]:
    """How many sections sit in each enrollment status right now."""
    rows = await session.execute(
        select(m.Section.enrollment_status, func.count())
        .join(m.Term, m.Term.id == m.Section.term_id)
        .where(m.Term.code == term_code)
        .group_by(m.Section.enrollment_status)
        .order_by(func.count().desc())
    )
    return [StatusCount(status=r[0], sections=int(r[1])) for r in rows]


async def most_in_demand(
    session: AsyncSession, term_code: str, limit: int = 15, min_capacity: int = 20
) -> list[CourseDemand]:
    """Courses where the most people want a seat, relative to seats available.

    Aggregated over a course's sections so a lecture split into discussions is
    counted once. ``min_capacity`` filters out seminars and independent studies,
    where a capacity of 2 makes the ratio meaningless.
    """
    rows = await session.execute(
        select(
            m.SubjectArea.code,
            m.Course.number,
            m.Course.title,
            func.sum(m.Section.enrollment_count),
            func.sum(m.Section.enrollment_capacity),
            func.sum(m.Section.waitlist_count),
            func.count(),
        )
        .select_from(m.Section)
        .join(m.Term, m.Term.id == m.Section.term_id)
        .join(m.Course, m.Course.id == m.Section.course_id)
        .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
        .where(m.Term.code == term_code)
        .group_by(m.SubjectArea.code, m.Course.number, m.Course.title)
        .having(func.sum(m.Section.enrollment_capacity) >= min_capacity)
        .order_by(
            (
                cast(
                    func.sum(m.Section.enrollment_count) + func.sum(m.Section.waitlist_count),
                    Integer,
                )
                * 1.0
                / func.nullif(func.sum(m.Section.enrollment_capacity), 0)
            ).desc()
        )
        .limit(limit)
    )
    return [
        CourseDemand(
            subject_area_code=r[0],
            course_number=r[1],
            title=r[2],
            enrolled=int(r[3] or 0),
            capacity=int(r[4] or 0),
            waitlisted=int(r[5] or 0),
            sections=int(r[6]),
        )
        for r in rows
    ]


async def subject_pressure(
    session: AsyncSession, term_code: str, limit: int = 15, min_sections: int = 10
) -> list[SubjectPressure]:
    """Share of each subject's sections that are not open."""
    closed = func.count().filter(m.Section.enrollment_status.in_(CLOSED_STATUSES))
    rows = await session.execute(
        select(m.SubjectArea.code, m.SubjectArea.name, func.count(), closed)
        .select_from(m.Section)
        .join(m.Term, m.Term.id == m.Section.term_id)
        .join(m.Course, m.Course.id == m.Section.course_id)
        .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
        .where(m.Term.code == term_code)
        .group_by(m.SubjectArea.code, m.SubjectArea.name)
        .having(func.count() >= min_sections)
        .order_by((cast(closed, Integer) * 1.0 / func.nullif(func.count(), 0)).desc())
        .limit(limit)
    )
    return [
        SubjectPressure(
            subject_area_code=r[0], name=r[1], sections=int(r[2]), closed_sections=int(r[3])
        )
        for r in rows
    ]


async def fastest_filling(
    session: AsyncSession, term_code: str, limit: int = 15
) -> list[FillSpeed]:
    """Sections that closed soonest after we started watching them.

    Measured from our first observation, not from when enrollment opened -- we
    cannot know the latter for a section we met mid-term.
    """
    first_seen = func.min(m.EnrollmentDatum.created_at)
    filled_at = func.min(m.EnrollmentDatum.created_at).filter(
        m.EnrollmentDatum.enrollment_status.in_(("Full", "Waitlist"))
    )

    inner = (
        select(
            m.EnrollmentDatum.section_id.label("section_id"),
            first_seen.label("first_seen"),
            filled_at.label("filled_at"),
        )
        .join(m.Section, m.Section.id == m.EnrollmentDatum.section_id)
        .join(m.Term, m.Term.id == m.Section.term_id)
        .where(m.Term.code == term_code)
        .group_by(m.EnrollmentDatum.section_id)
        .subquery()
    )

    rows = await session.execute(
        select(
            m.SubjectArea.code,
            m.Course.number,
            m.Section.section_label,
            m.Section.enrollment_capacity,
            func.extract("epoch", inner.c.filled_at - inner.c.first_seen) / 3600.0,
        )
        .select_from(inner)
        .join(m.Section, m.Section.id == inner.c.section_id)
        .join(m.Course, m.Course.id == m.Section.course_id)
        .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
        .where(
            and_(
                inner.c.filled_at.isnot(None),
                inner.c.filled_at > inner.c.first_seen,
            )
        )
        .order_by((inner.c.filled_at - inner.c.first_seen).asc())
        .limit(limit)
    )
    return [
        FillSpeed(
            subject_area_code=r[0],
            course_number=r[1],
            section_label=r[2],
            capacity=int(r[3] or 0),
            hours_to_full=float(r[4] or 0.0),
        )
        for r in rows
    ]


# --------------------------------------------------------------------------
# Course detail
# --------------------------------------------------------------------------


async def course_fill_curves(
    session: AsyncSession, subject_area_code: str, course_number: str, term_code: str
) -> list[SectionSeries]:
    """Every section's enrollment history for one course in one term."""
    sections = (
        await session.execute(
            select(
                m.Section.id,
                m.Section.section_label,
                m.Section.instructors,
                m.Section.enrollment_capacity,
                m.Section.enrollment_status,
                m.Section.index,
            )
            .join(m.Term, m.Term.id == m.Section.term_id)
            .join(m.Course, m.Course.id == m.Section.course_id)
            .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
            .where(
                m.SubjectArea.code == subject_area_code,
                m.Course.number == course_number,
                m.Term.code == term_code,
            )
            .order_by(m.Section.index, m.Section.section_label)
        )
    ).all()
    if not sections:
        return []

    ids = [row[0] for row in sections]
    history = (
        await session.execute(
            select(
                m.EnrollmentDatum.section_id,
                m.EnrollmentDatum.created_at,
                m.EnrollmentDatum.enrollment_count,
                m.EnrollmentDatum.enrollment_capacity,
                m.EnrollmentDatum.waitlist_count,
                m.EnrollmentDatum.enrollment_status,
            )
            .where(m.EnrollmentDatum.section_id.in_(ids))
            .order_by(m.EnrollmentDatum.section_id, m.EnrollmentDatum.created_at)
        )
    ).all()

    points: dict[int, list[Observation]] = {i: [] for i in ids}
    for section_id, at, enrolled, capacity, waitlisted, status in history:
        points[section_id].append(
            Observation(
                at=at,
                enrolled=enrolled,
                capacity=capacity,
                waitlisted=waitlisted,
                status=status,
            )
        )

    return [
        SectionSeries(
            section_id=row[0],
            section_label=row[1],
            instructors=tuple(row[2] or ()),
            capacity=int(row[3] or 0),
            status=row[4],
            points=tuple(points[row[0]]),
        )
        for row in sections
    ]


async def course_term_peaks(
    session: AsyncSession, subject_area_code: str, course_number: str
) -> list[TermPeak]:
    """A course's high-water mark in each term we have history for.

    This is the cross-term "is it getting more popular" view, and it needs at
    least two terms of collected data before it says anything.
    """
    rows = await session.execute(
        select(
            m.Term.code,
            m.Term.name,
            m.Term.position,
            func.max(m.EnrollmentDatum.enrollment_count),
            func.max(m.EnrollmentDatum.enrollment_capacity),
            func.max(m.EnrollmentDatum.waitlist_count),
        )
        .select_from(m.EnrollmentDatum)
        .join(m.Section, m.Section.id == m.EnrollmentDatum.section_id)
        .join(m.Term, m.Term.id == m.Section.term_id)
        .join(m.Course, m.Course.id == m.Section.course_id)
        .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
        .where(
            m.SubjectArea.code == subject_area_code,
            m.Course.number == course_number,
        )
        .group_by(m.Term.code, m.Term.name, m.Term.position)
        # Oldest first, so the chart reads left to right through time.
        .order_by(m.Term.position.desc())
    )
    return [
        TermPeak(
            term_code=r[0],
            term_name=r[1],
            position=int(r[2]),
            peak_enrolled=int(r[3] or 0),
            capacity=int(r[4] or 0),
            peak_waitlisted=int(r[5] or 0),
        )
        for r in rows
    ]


async def tracked_courses(
    session: AsyncSession, term_code: str, limit: int = 200
) -> list[tuple[str, str, str]]:
    """Courses with any recorded history, for the index page."""
    rows = await session.execute(
        select(m.SubjectArea.code, m.Course.number, m.Course.title)
        .select_from(m.EnrollmentDatum)
        .join(m.Section, m.Section.id == m.EnrollmentDatum.section_id)
        .join(m.Term, m.Term.id == m.Section.term_id)
        .join(m.Course, m.Course.id == m.Section.course_id)
        .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
        .where(m.Term.code == term_code)
        .group_by(m.SubjectArea.code, m.Course.number, m.Course.title)
        .order_by(func.count().desc())
        .limit(limit)
    )
    return [(r[0], r[1], r[2]) for r in rows]


def status_tone(status: str) -> str:
    """Map an enrollment status onto a status-palette role.

    Status colours are reserved and always ship with a label, never alone.
    """
    return {
        "Open": "good",
        "Waitlist": "warning",
        "Full": "critical",
        "Closed": "critical",
        "Cancelled": "serious",
    }.get(status, "neutral")
