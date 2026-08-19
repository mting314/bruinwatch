"""Backfill the catalog for past terms.

The registrar serves any term code you ask for, back to Fall 1999 -- far beyond
the eight terms its dropdown advertises. This walks those terms and records what
was offered, by whom, at what capacity, and how full each section ended up.

**It cannot recover enrollment history.** The archive returns a single frozen
snapshot per section, not a time series: there is exactly one observation, and
it is the state the registrar last recorded. Fill curves, time-to-full and
anything else about *how* demand built only exist for terms the live scraper
watched. A backfill is therefore a different dataset from the hourly sweep, not
an extension of it, and :func:`backfill_term` labels its observations
accordingly by writing them with the term's own end date rather than "now".

Politeness is the binding constraint, not throughput. One term is roughly
11,000 requests; the whole archive is about 1.5 million. The rate limit lives on
the client (``requests_per_second``) and defaults here to something a public
university endpoint will not notice.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import models as m
from ..db.session import transaction
from ..registrar import RegistrarClient
from ..registrar.types import SEASON_ORDER, Term, calendar_year, term_position
from .sync import sync_courses_for_subject, sync_subject_areas

log = structlog.get_logger(__name__)

#: Earliest term the registrar still serves. Probed empirically: 99F returns
#: sections, 98F and everything before it return an empty body.
EARLIEST_TERM = "99F"

SEASON_NAMES = {"W": "Winter", "S": "Spring", "1": "Summer Sessions", "2": "Summer", "F": "Fall"}


def term_name(code: str) -> str:
    return f"{SEASON_NAMES[code[-1]]} {calendar_year(code)}"


def expand_terms(start: str, end: str) -> list[str]:
    """Every term code from ``start`` to ``end`` inclusive, newest first.

    Both bounds are term codes; order between them does not matter.
    """
    # Smaller position means newer, so sorting ascending puts the newer bound
    # first regardless of which way round the caller passed them.
    newest, oldest = sorted([start.upper(), end.upper()], key=term_position)
    candidates = [
        f"{year % 100:02d}{season}"
        for year in range(calendar_year(oldest), calendar_year(newest) + 1)
        for season in SEASON_ORDER
    ]
    inside = [
        code
        for code in candidates
        if term_position(newest) <= term_position(code) <= term_position(oldest)
    ]
    return sorted(inside, key=term_position)


@dataclasses.dataclass(frozen=True, slots=True)
class BackfillResult:
    terms: int = 0
    subjects: int = 0
    courses: int = 0
    sections: int = 0
    skipped: int = 0

    def __add__(self, other: BackfillResult) -> BackfillResult:
        return BackfillResult(
            self.terms + other.terms,
            self.subjects + other.subjects,
            self.courses + other.courses,
            self.sections + other.sections,
            self.skipped + other.skipped,
        )


async def ensure_term(factory: async_sessionmaker[AsyncSession], code: str) -> None:
    """Create the term row for a historical term.

    ``sync_terms`` only ever sees the eight terms in the dropdown, so a
    backfilled term has to be inserted here. It is never marked active: the
    poller must not spend requests on a term that ended years ago.
    """
    async with transaction(factory) as session:
        await session.execute(
            pg_insert(m.Term)
            .values(
                code=code,
                name=term_name(code),
                position=term_position(code),
                is_current=False,
                is_active=False,
            )
            .on_conflict_do_nothing(index_elements=["code"])
        )


async def completed_units(session: AsyncSession, term_code: str) -> set[str]:
    """Subject codes already backfilled for a term."""
    rows = await session.execute(
        select(m.BackfillProgress.subject_area_code).where(
            m.BackfillProgress.term_code == term_code
        )
    )
    return set(rows.scalars().all())


async def backfill_term(
    client: RegistrarClient,
    factory: async_sessionmaker[AsyncSession],
    term_code: str,
    *,
    resume: bool = True,
    on_progress: Callable[[str, str, int], Awaitable[None]] | None = None,
) -> BackfillResult:
    """Walk one term's subjects and record its catalog."""
    await ensure_term(factory, term_code)
    await sync_subject_areas(client, factory, term_code)

    async with factory() as session:
        subjects = list(
            (await session.execute(select(m.SubjectArea.code).order_by(m.SubjectArea.code)))
            .scalars()
            .all()
        )
        done = await completed_units(session, term_code) if resume else set()

    result = BackfillResult(terms=1)
    for code in subjects:
        if code in done:
            result = result + BackfillResult(skipped=1)
            continue

        courses = await sync_courses_for_subject(client, factory, code, term_code)
        sections = await _backfill_sections(client, factory, code, term_code)

        async with transaction(factory) as session:
            await session.execute(
                pg_insert(m.BackfillProgress)
                .values(
                    term_code=term_code,
                    subject_area_code=code,
                    courses=courses,
                    sections=sections,
                )
                .on_conflict_do_update(
                    index_elements=["term_code", "subject_area_code"],
                    set_={"courses": courses, "sections": sections},
                )
            )

        result = result + BackfillResult(subjects=1, courses=courses, sections=sections)
        if on_progress is not None:
            await on_progress(term_code, code, sections)

    log.info(
        "backfilled_term",
        term=term_code,
        subjects=result.subjects,
        courses=result.courses,
        sections=result.sections,
        skipped=result.skipped,
    )
    return result


async def _backfill_sections(
    client: RegistrarClient,
    factory: async_sessionmaker[AsyncSession],
    subject_area_code: str,
    term_code: str,
) -> int:
    """Fetch sections for every course this subject offers in the term."""
    from ..registrar.types import Course as CourseDTO
    from .sync import sync_course_sections

    async with factory() as session:
        rows = await session.execute(
            select(m.Course.number, m.CourseTerm.section_indices)
            .select_from(m.CourseTerm)
            .join(m.Course, m.Course.id == m.CourseTerm.course_id)
            .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
            .join(m.Term, m.Term.id == m.CourseTerm.term_id)
            .where(m.SubjectArea.code == subject_area_code, m.Term.code == term_code)
        )
        courses = [(r[0], tuple(r[1] or ["%"])) for r in rows]

    total = 0
    for number, indices in courses:
        outcome = await sync_course_sections(
            client,
            factory,
            CourseDTO(subject_area_code=subject_area_code, number=number, section_indices=indices),
            term_code,
            # One frozen snapshot per section is worth recording -- it is the
            # end state of that term -- but there is no series to build.
            record_history=True,
        )
        total += outcome.sections_seen
    return total


async def backfill(
    client: RegistrarClient,
    factory: async_sessionmaker[AsyncSession],
    terms: list[str],
    *,
    resume: bool = True,
    on_progress: Callable[[str, str, int], Awaitable[None]] | None = None,
) -> BackfillResult:
    """Backfill a list of terms, newest first."""
    result = BackfillResult()
    for code in terms:
        result = result + await backfill_term(
            client, factory, code, resume=resume, on_progress=on_progress
        )
    return result


def validate_terms(terms: list[str]) -> list[str]:
    """Reject codes the registrar will not serve, so a long run fails early."""
    floor = term_position(EARLIEST_TERM)
    bad = []
    for code in terms:
        try:
            Term(code=code)
        except ValueError:
            bad.append(f"{code} (malformed)")
            continue
        if term_position(code) > floor:
            bad.append(f"{code} (before {EARLIEST_TERM}, the registrar's earliest)")
    if bad:
        raise ValueError("unusable terms: " + ", ".join(bad))
    return terms


def estimate(terms: list[str], requests_per_second: float) -> dict[str, float]:
    """Rough cost of a run, for the CLI to print before it starts.

    Per-term figures are measured: ~168 subjects, ~62 courses each, ~11k
    requests, ~10.8 KB per response.
    """
    requests_per_term = 11_000
    kb_per_request = 10.8
    total_requests = len(terms) * requests_per_term
    return {
        "terms": len(terms),
        "requests": total_requests,
        "hours": total_requests / requests_per_second / 3600,
        "gigabytes": total_requests * kb_per_request / 1_048_576,
    }


def term_end_date(code: str) -> dt.date:
    """Approximate last day of a term, for labelling frozen observations."""
    year = calendar_year(code)
    return {
        "W": dt.date(year, 3, 20),
        "S": dt.date(year, 6, 12),
        "1": dt.date(year, 8, 1),
        "2": dt.date(year, 9, 12),
        "F": dt.date(year, 12, 12),
    }[code[-1]]
