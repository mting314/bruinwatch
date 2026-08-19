"""Fetch-and-parse routines: the only place client and parsers meet.

Each function mirrors one of hotseat.io's ``fetch-*`` lambdas, but they run as
coroutines inside the bot process rather than as separately scheduled functions.
"""

from __future__ import annotations

import asyncio

import structlog

from .client import RegistrarClient, RegistrarError
from .model import build_model, build_subject_model, catalog_number, is_interesting_course
from .parsing import (
    has_more_pages,
    parse_course_summary,
    parse_course_titles,
    parse_section_details,
    parse_subject_areas,
    parse_terms,
)
from .types import Course, Section, SubjectArea, Term

log = structlog.get_logger(__name__)


class FetchFailures:
    """Counts requests a caller chose to skip rather than abort on.

    The scrapers swallow per-course failures so one bad course cannot kill a
    sweep. For a multi-hour backfill that is dangerous on its own: a run that
    was throttled halfway through would otherwise report success while missing
    thousands of courses. Pass one of these in to find out.
    """

    __slots__ = ("courses", "pages", "reasons")

    def __init__(self) -> None:
        self.courses = 0
        self.pages = 0
        self.reasons: dict[str, int] = {}

    def record(self, kind: str, error: Exception) -> None:
        if kind == "course":
            self.courses += 1
        else:
            self.pages += 1
        name = type(error).__name__
        self.reasons[name] = self.reasons.get(name, 0) + 1

    @property
    def total(self) -> int:
        return self.courses + self.pages

    def __bool__(self) -> bool:
        return self.total > 0

    def summary(self) -> str:
        parts = ", ".join(f"{n}x {k}" for k, n in sorted(self.reasons.items()))
        return f"{self.courses} courses and {self.pages} catalog pages skipped ({parts})"


#: Guard against a pagination bug turning into an unbounded request loop.
MAX_COURSE_PAGES = 40


async def fetch_terms(client: RegistrarClient) -> list[Term]:
    """Every term the registrar currently publishes."""
    return parse_terms(await client.get_soc_home())


async def fetch_subject_areas(client: RegistrarClient, term: str) -> list[SubjectArea]:
    """Every subject area offered in a term.

    The list is only exposed as a bootstrap blob on a search-results page, so we
    request an arbitrary subject's results purely to read it back.
    """
    markup = await client.get_results_page(term, "MATH")
    areas = parse_subject_areas(markup)
    log.info("fetched_subject_areas", term=term, count=len(areas))
    return areas


async def fetch_courses_for_subject(
    client: RegistrarClient,
    subject_area_code: str,
    term: str,
    failures: FetchFailures | None = None,
) -> list[Course]:
    """Page through a subject area's course list.

    The registrar has no page count; it just serves a body with no courses in it
    once you walk off the end.
    """
    model = build_subject_model(subject_area_code, term)
    courses: list[Course] = []
    seen: set[str] = set()

    for page in range(1, MAX_COURSE_PAGES + 1):
        try:
            markup = await client.get_course_titles(model, page)
        except RegistrarError as exc:
            log.warning("course_page_failed", subject=subject_area_code, page=page, error=str(exc))
            if failures is not None:
                failures.record("page", exc)
            break
        if not has_more_pages(markup):
            break
        page_courses = [
            course
            for course in parse_course_titles(markup, subject_area_code)
            if is_interesting_course(subject_area_code, course.number)
        ]
        # Defensive: if the endpoint starts ignoring pageNumber we would
        # otherwise loop until MAX_COURSE_PAGES re-collecting page 1.
        fresh = [c for c in page_courses if c.number not in seen]
        if not fresh:
            break
        seen.update(c.number for c in fresh)
        courses.extend(fresh)
    else:
        log.warning("course_pagination_capped", subject=subject_area_code, pages=MAX_COURSE_PAGES)

    log.info("fetched_courses", subject=subject_area_code, term=term, count=len(courses))
    return courses


async def fetch_sections_for_course(
    client: RegistrarClient,
    course: Course,
    term: str,
    *,
    with_details: bool = False,
    failures: FetchFailures | None = None,
) -> list[Section]:
    """Fetch every section of a course, across all of its listing indices."""
    sections: list[Section] = []
    for index in course.section_indices or ("%",):
        model = build_model(course.subject_area_code, course.number, term, class_number=index)
        try:
            markup = await client.get_course_summary(model)
        except RegistrarError as exc:
            log.warning("section_fetch_failed", course=course.short_title, error=str(exc))
            if failures is not None:
                failures.record("course", exc)
            continue
        sections.extend(parse_course_summary(markup, term, course.subject_area_code, course.number))

    if with_details and sections:
        sections = await _attach_details(client, course, term, sections)
    return sections


async def _attach_details(
    client: RegistrarClient, course: Course, term: str, sections: list[Section]
) -> list[Section]:
    """Enrich sections with website and final-exam info from the tooltip."""
    import dataclasses

    async def enrich(section: Section) -> Section:
        try:
            markup = await client.get_class_detail_tooltip(
                term=term,
                subject_area_code=course.subject_area_code,
                catalog_number=catalog_number(course.number),
                registrar_id=section.registrar_id,
                index=section.index,
            )
        except RegistrarError as exc:
            log.debug("detail_fetch_failed", section=section.registrar_id, error=str(exc))
            return section
        details = parse_section_details(markup)
        return dataclasses.replace(
            section,
            website=details.get("website") or section.website,  # type: ignore[arg-type]
            final_start=details.get("final_start") or section.final_start,  # type: ignore[arg-type]
            final_end=details.get("final_end") or section.final_end,  # type: ignore[arg-type]
        )

    return list(await asyncio.gather(*(enrich(section) for section in sections)))


async def fetch_sections_by_registrar_id(
    client: RegistrarClient,
    term: str,
    subject_area_code: str,
    course_number: str,
    registrar_ids: set[str],
) -> dict[str, Section]:
    """Fetch a course's sections, keyed by registrar ID, filtered to those wanted.

    Watched sections are polled a course at a time rather than a section at a
    time: one request returns every section of the course, so a user watching
    both the lecture and its discussion costs one request, not two.
    """
    course = Course(subject_area_code=subject_area_code, number=course_number)
    sections = await fetch_sections_for_course(client, course, term)
    return {s.registrar_id: s for s in sections if s.registrar_id in registrar_ids}
