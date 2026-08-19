"""Pure HTML -> dataclass parsing for the Schedule of Classes.

Nothing in here performs I/O, so every function is directly unit testable
against the saved fixtures in ``tests/fixtures/``. All parsers are total: they
return a value (possibly with ``UNKNOWN`` status) rather than ``None``, because
the previous implementation returned ``None`` on an unrecognized status string
and callers dereferenced it immediately.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
import json
import re

from bs4 import BeautifulSoup, Tag

from .types import (
    Course,
    EnrollmentNumbers,
    EnrollmentStatus,
    Section,
    SubjectArea,
    Term,
    WaitlistStatus,
    term_position,
)

# --------------------------------------------------------------------------
# Enrollment status
# --------------------------------------------------------------------------

# Ordered: the first pattern that matches wins, so the more specific
# "WaitlistClass Full (n)" must be tried before the bare "Waitlist".
_ENROLLMENT_PATTERNS: tuple[tuple[EnrollmentStatus, re.Pattern[str]], ...] = (
    (EnrollmentStatus.TENTATIVE, re.compile(r"^Tentative")),
    (EnrollmentStatus.CANCELLED, re.compile(r"^Cancelled")),
    (
        EnrollmentStatus.CLOSED,
        re.compile(
            r"^Closed by Dept[a-zA-Z,/\-&' ]*"
            r"\((?P<capacity>\d+) capacity, (?P<count>\d+) enrolled, (?P<waitlisted>\d+) waitlisted\)"
        ),
    ),
    (EnrollmentStatus.CLOSED, re.compile(r"^Closed by Dept")),
    (
        EnrollmentStatus.FULL,
        re.compile(
            r"^ClosedClass Full \((?P<capacity>\d+)\)"
            r"(?:, Over Enrolled By (?P<over>\d+))?"
        ),
    ),
    (
        EnrollmentStatus.WAITLIST,
        re.compile(
            r"^WaitlistClass Full \((?P<capacity>\d+)\)"
            r"(?:, Over Enrolled By (?P<over>\d+))?"
        ),
    ),
    (
        EnrollmentStatus.OPEN,
        re.compile(r"^Open(?P<count>\d+) of (?P<capacity>\d+) Enrolled(?P<left>\d+) Spots? Left"),
    ),
    (EnrollmentStatus.WAITLIST, re.compile(r"^Waitlist\s*$")),
    (EnrollmentStatus.CLOSED, re.compile(r"^Closed")),
)

_WAITLIST_PATTERNS: tuple[tuple[WaitlistStatus, re.Pattern[str]], ...] = (
    (WaitlistStatus.NONE, re.compile(r"^No Waitlist")),
    (WaitlistStatus.FULL, re.compile(r"^Waitlist Full \((?P<capacity>\d+)\)")),
    (
        WaitlistStatus.CONTACT_INSTRUCTOR,
        re.compile(r"^(?P<count>\d+) Waitlisted, Contact Instructor/Department"),
    ),
    (WaitlistStatus.OPEN, re.compile(r"^(?P<count>\d+) of (?P<capacity>\d+) Taken")),
)


def _squash(text: str) -> str:
    """Collapse whitespace the way ``.text`` on a browser-rendered node would."""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _int(value: str | None) -> int:
    return int(value) if value else 0


def parse_enrollment_status(raw: str) -> tuple[EnrollmentStatus, int, int]:
    """Parse the status column into ``(status, count, capacity)``.

    Some registrar strings omit numbers that are implied by the status. A class
    reported as ``ClosedClass Full (45), Over Enrolled By 3`` states neither the
    count nor, strictly, that it is at capacity -- but "Full" means count equals
    capacity, plus any overenrollment.
    """
    text = _squash(raw)
    for status, pattern in _ENROLLMENT_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        groups = match.groupdict()
        capacity = _int(groups.get("capacity"))
        count = _int(groups.get("count"))

        if status in (EnrollmentStatus.FULL, EnrollmentStatus.WAITLIST) and "count" not in groups:
            # Full implies count == capacity, and over-enrollment pushes past it.
            count = capacity + _int(groups.get("over"))
        return status, count, capacity

    return EnrollmentStatus.UNKNOWN, 0, 0


def parse_waitlist_status(raw: str) -> tuple[WaitlistStatus, int, int]:
    """Parse the waitlist column into ``(status, count, capacity)``."""
    text = _squash(raw)
    for status, pattern in _WAITLIST_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        groups = match.groupdict()
        capacity = _int(groups.get("capacity"))
        count = _int(groups.get("count"))
        if status is WaitlistStatus.FULL:
            count = capacity
        return status, count, capacity

    return WaitlistStatus.UNKNOWN, 0, 0


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

_ROW_ID_RE = re.compile(r"^(?P<registrar_id>\d+)_(?P<path>.+)$")
_SECTION_LABEL_RE = re.compile(r"^(?P<format>[A-Za-z ]+?)\s*(?P<index>\d+)(?P<suffix>[A-Z]*)$")
_FINAL_TIME_RE = re.compile(r"(\d{1,2}(?::\d{2})?(?:am|pm))-(\d{1,2}(?::\d{2})?(?:am|pm))")
_SUMMER_TITLE_RE = re.compile(
    r"Session (?P<session>[A-Z]\d*)\b.*?Duration (?P<weeks>\d+) weeks?", re.IGNORECASE
)


def _lines(node: Tag | None) -> tuple[str, ...]:
    """Split a cell that uses ``<br/>`` as a record separator into its lines.

    The registrar packs multiple meeting patterns into one cell separated by
    ``<br/>``, and sprinkles ``<wbr/>`` inside times (``4pm<wbr/>-5:50pm``).
    """
    if node is None:
        return ()
    for wbr in node.find_all("wbr"):
        wbr.decompose()
    text = node.decode_contents()
    parts = re.split(r"<br\s*/?>", text)
    out = []
    for part in parts:
        cleaned = _squash(BeautifulSoup(part, "lxml").get_text())
        if cleaned:
            out.append(cleaned)
    return tuple(out)


def _select_by_id_suffix(row: Tag, suffix: str) -> Tag | None:
    node = row.select_one(f'[id$="{suffix}"]')
    return node if isinstance(node, Tag) else None


def parse_section_label(label: str) -> tuple[str, int]:
    """Split ``"Lec 1"`` into ``("Lec", 1)``; ``"Dis 1A"`` into ``("Dis", 1)``."""
    match = _SECTION_LABEL_RE.match(_squash(label))
    if match is None:
        return _squash(label), 0
    return match.group("format").strip(), int(match.group("index"))


def parse_section_row(
    row: Tag, term: str, subject_area_code: str, course_number: str
) -> Section | None:
    """Parse one ``.data_row`` from a ``GetCourseSummary`` response."""
    row_id = row.get("id")
    if not isinstance(row_id, str):
        return None
    id_match = _ROW_ID_RE.match(row_id)
    if id_match is None:
        return None
    registrar_id = id_match.group("registrar_id")

    # The section cell renders the label twice -- once in a <p> for wide
    # viewports and again in a div for narrow ones. Take the first only.
    label_node = row.select_one('[id$="-section"] p') or _select_by_id_suffix(row, "-section")
    label = _squash(label_node.get_text()) if label_node else ""
    section_format, index = parse_section_label(label)

    status_node = _select_by_id_suffix(row, "-status_data")
    waitlist_node = _select_by_id_suffix(row, "-waitlist_data")
    status, count, capacity = parse_enrollment_status(status_node.get_text() if status_node else "")
    wl_status, wl_count, wl_capacity = parse_waitlist_status(
        waitlist_node.get_text() if waitlist_node else ""
    )

    units_node = _select_by_id_suffix(row, "-units_data")

    return Section(
        registrar_id=registrar_id,
        term_code=term,
        subject_area_code=subject_area_code,
        course_number=course_number,
        section_label=label,
        index=index,
        format=section_format,
        # The days cell is duplicated for the responsive layout; take the first.
        days=_lines(row.select_one('[id$="-days_data"] p')),
        times=_lines(row.select_one('[id$="-time_data"] > p')),
        locations=_lines(_select_by_id_suffix(row, "-location_data")),
        instructors=_lines(row.select_one('[id$="-instructor_data"] p')),
        units=_squash(units_node.get_text()) if units_node else "",
        enrollment=EnrollmentNumbers(
            enrollment_status=status,
            enrollment_count=count,
            enrollment_capacity=capacity,
            waitlist_status=wl_status,
            waitlist_count=wl_count,
            waitlist_capacity=wl_capacity,
        ),
    )


def parse_course_summary(
    markup: str, term: str, subject_area_code: str, course_number: str
) -> list[Section]:
    """Parse every section out of a ``GetCourseSummary`` response.

    Returns an empty list for the "No results available based off your filter
    criteria." body the registrar serves for courses not offered in the term.
    """
    soup = BeautifulSoup(markup, "lxml")
    sections = []
    for row in soup.select("div.data_row.primary-row"):
        section = parse_section_row(row, term, subject_area_code, course_number)
        if section is None:
            continue
        summer = _summer_info_for(row)
        if summer is not None:
            session, weeks = summer
            section = dataclasses.replace(
                section, summer_session=session, summer_duration_weeks=weeks
            )
        sections.append(section)
    return sections


def _summer_info_for(row: Tag) -> tuple[str, int] | None:
    """Find the summer session a row belongs to, if any.

    Summer responses interleave session headers with the rows they introduce::

        <div class="summer-session-title"><p>Session A8: Meets from 6/22-8/14: Duration 8 weeks</p></div>
        <div class="row-fluid data_row primary-row ...">...</div>

    so the owning session is the nearest such header *before* the row.
    """
    for previous in row.find_all_previous("div", class_="summer-session-title"):
        match = _SUMMER_TITLE_RE.search(previous.get_text())
        if match:
            return match.group("session"), int(match.group("weeks"))
    return None


# --------------------------------------------------------------------------
# Section detail tooltip
# --------------------------------------------------------------------------


def parse_section_details(markup: str) -> dict[str, object]:
    """Extract website and final-exam times from a ``ClassDetailTooltip`` body."""
    soup = BeautifulSoup(markup, "lxml")
    details: dict[str, object] = {}

    for row in soup.select(".grade_type_content p"):
        header = row.select_one(".grade_type_content_header")
        value = row.select_one(".grade_type_content_text")
        if header is None or value is None:
            continue
        if "Class Webpage" in header.get_text():
            website = _squash(value.get_text())
            link = value.find("a")
            if isinstance(link, Tag) and isinstance(link.get("href"), str):
                website = str(link["href"])
            if website and website != "N/A":
                details["website"] = website

    final_table = soup.select_one("table.final_exam_content")
    if final_table is not None:
        cells = [_squash(td.get_text()) for td in final_table.select("tbody td")]
        if len(cells) >= 3:
            start, end = _parse_final_exam(cells[0], cells[2])
            if start is not None:
                details["final_start"] = start
            if end is not None:
                details["final_end"] = end
    return details


def _parse_final_exam(
    date_text: str, time_text: str
) -> tuple[dt.datetime | None, dt.datetime | None]:
    match = _FINAL_TIME_RE.search(time_text.replace(" ", ""))
    if match is None:
        return None, None
    for date_format in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%b %d %Y"):
        try:
            day = dt.datetime.strptime(date_text, date_format)
            break
        except ValueError:
            continue
    else:
        return None, None

    def combine(clock: str) -> dt.datetime | None:
        for clock_format in ("%I:%M%p", "%I%p"):
            try:
                parsed = dt.datetime.strptime(clock, clock_format)
            except ValueError:
                continue
            return day.replace(hour=parsed.hour, minute=parsed.minute)
        return None

    return combine(match.group(1)), combine(match.group(2))


# --------------------------------------------------------------------------
# Catalog: terms, subject areas, courses
# --------------------------------------------------------------------------

# Anchor on the JSON array itself: SearchPanelSetup takes several more
# single-quoted arguments after it, and a lazy match to the next "')" runs
# straight past the end of the array into them.
_SUBJECT_JSON_RE = re.compile(r"SearchPanelSetup\('(?P<json>\[.*?\])\s*'", re.DOTALL)
_COURSE_TITLE_RE = re.compile(r"^(?P<number>\S+)\s*-\s*(?P<title>.+)$")
_ADD_COURSE_DATA_RE = re.compile(
    r"Iwe_ClassSearch_SearchResults\.AddToCourseData\(\s*\"[^\"]*\"\s*,\s*(?P<model>\{.*?\})\s*\)"
)


def parse_terms(markup: str) -> list[Term]:
    """Parse the term dropdown from the SOC home page, newest first.

    Two things here are load-bearing and must not be recomputed downstream:

    * **Order.** The registrar lists terms reverse-chronologically
      (``27S 27W 26F 262 261 26S ...``). Term codes do *not* sort that way as
      strings -- within a year the suffixes run Winter, Spring, Fall but sort
      ``F < S < W``. :attr:`Term.position` is computed from the code by
      :func:`~bruinwatch.registrar.types.term_position`, which reproduces the
      dropdown's order while also placing backfilled terms the dropdown never
      lists.
    * **Which term is current.** The registrar marks it ``selected``. Guessing
      it from the codes gets it wrong.
    """
    soup = BeautifulSoup(markup, "lxml")
    terms: list[Term] = []
    for option in soup.select("option.select_term"):
        code = option.get("value")
        if not isinstance(code, str):
            continue
        name = option.get("data-yearText") or option.get_text()
        try:
            terms.append(
                Term(
                    code=code,
                    name=_squash(str(name)),
                    position=term_position(code),
                    is_current=option.has_attr("selected"),
                )
            )
        except ValueError:
            continue  # ignore placeholder options like "Select a term"
    return terms


def parse_subject_areas(markup: str) -> list[SubjectArea]:
    """Parse the subject-area list out of the ``SearchPanelSetup`` bootstrap.

    The page embeds it as HTML-escaped JSON inside a single-quoted JS string
    (``[{&quot;label&quot;:&quot;Aerospace Studies (AERO ST)&quot;, ...}]``).
    """
    match = _SUBJECT_JSON_RE.search(markup)
    if match is None:
        return []
    try:
        entries = json.loads(html.unescape(match.group("json")))
    except json.JSONDecodeError:
        return []

    areas = []
    for entry in entries:
        code = str(entry.get("value", "")).strip()
        label = str(entry.get("label", "")).strip()
        if not code:
            continue
        # "Aerospace Studies (AERO ST)" -> "Aerospace Studies"
        name = re.sub(r"\s*\([^)]*\)\s*$", "", label) or code
        areas.append(SubjectArea(code=code, name=name))
    return areas


def parse_course_titles(markup: str, subject_area_code: str) -> list[Course]:
    """Parse one page of ``CourseTitlesView`` into courses.

    Also collects the per-course section indices: courses listed more than once
    under distinct ``ClassNumber`` values need one summary request per index,
    while the common case is the single ``"%"`` wildcard.
    """
    soup = BeautifulSoup(markup, "lxml")
    indices: dict[str, list[str]] = {}
    for script in soup.find_all("script"):
        for match in _ADD_COURSE_DATA_RE.finditer(script.decode_contents()):
            try:
                model = json.loads(match.group("model"))
            except json.JSONDecodeError:
                continue
            path = str(model.get("Path", ""))
            class_number = str(model.get("ClassNumber", "%"))
            if not model.get("IsRoot", False):
                continue
            indices.setdefault(path, [])
            if class_number not in indices[path]:
                indices[path].append(class_number)

    courses = []
    for heading in soup.select("div.class-title"):
        heading_id = heading.get("id")
        button = heading.select_one('[id$="-title"]')
        if not isinstance(heading_id, str) or button is None:
            continue
        title_match = _COURSE_TITLE_RE.match(_squash(button.get_text()))
        if title_match is None:
            continue
        courses.append(
            Course(
                subject_area_code=subject_area_code,
                number=title_match.group("number"),
                title=title_match.group("title"),
                section_indices=tuple(indices.get(heading_id) or ["%"]),
            )
        )
    return courses


def has_more_pages(markup: str) -> bool:
    """Whether a ``CourseTitlesView`` response contained any courses at all.

    The registrar signals "past the end" by returning an empty body rather than
    a page count, so pagination stops when a page yields nothing.
    """
    return bool(markup.strip()) and "class-title" in markup
