"""Parser tests against real Schedule of Classes responses.

The status strings in ``test_parse_*_status`` were all observed live on the
registrar (Fall 2026) except the rare ones noted, which come from hotseat.io's
regression corpus.
"""

from __future__ import annotations

import pytest

from bruinwatch.registrar.parsing import (
    has_more_pages,
    parse_course_summary,
    parse_course_titles,
    parse_enrollment_status,
    parse_section_details,
    parse_section_label,
    parse_subject_areas,
    parse_terms,
    parse_waitlist_status,
)
from bruinwatch.registrar.types import EnrollmentStatus, WaitlistStatus

# --------------------------------------------------------------------------
# Status strings
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "status", "count", "capacity"),
    [
        ("Open143 of 237 Enrolled94 Spots Left", EnrollmentStatus.OPEN, 143, 237),
        ("Open108 of 232 Enrolled124 Spots Left", EnrollmentStatus.OPEN, 108, 232),
        ("Open19 of 20 Enrolled1 Spot Left", EnrollmentStatus.OPEN, 19, 20),
        ("ClosedClass Full (120)", EnrollmentStatus.FULL, 120, 120),
        ("ClosedClass Full (18), Over Enrolled By 1", EnrollmentStatus.FULL, 19, 18),
        (
            "Closed by Dept Computer Science (0 capacity, 0 enrolled, 0 waitlisted)",
            EnrollmentStatus.CLOSED,
            0,
            0,
        ),
        (
            "Closed by Dept English (0 capacity, 3 enrolled, 0 waitlisted)",
            EnrollmentStatus.CLOSED,
            3,
            0,
        ),
        # Rarer states, from hotseat.io's corpus.
        ("Waitlist", EnrollmentStatus.WAITLIST, 0, 0),
        ("WaitlistClass Full (45)", EnrollmentStatus.WAITLIST, 45, 45),
        ("WaitlistClass Full (45), Over Enrolled By 3", EnrollmentStatus.WAITLIST, 48, 45),
        ("Tentative", EnrollmentStatus.TENTATIVE, 0, 0),
        ("Cancelled", EnrollmentStatus.CANCELLED, 0, 0),
    ],
)
def test_parse_enrollment_status(raw, status, count, capacity):
    assert parse_enrollment_status(raw) == (status, count, capacity)


def test_parse_enrollment_status_is_total():
    """Regression: this used to return None and callers dereferenced it."""
    assert parse_enrollment_status("something the registrar invented") == (
        EnrollmentStatus.UNKNOWN,
        0,
        0,
    )
    assert parse_enrollment_status("") == (EnrollmentStatus.UNKNOWN, 0, 0)


def test_parse_enrollment_status_tolerates_registrar_whitespace():
    # The status cell arrives with newlines and non-breaking spaces baked in.
    assert parse_enrollment_status("\n  Open143 of 237 Enrolled94 Spots Left \r\n") == (
        EnrollmentStatus.OPEN,
        143,
        237,
    )


def test_closed_by_dept_matches_departments_with_punctuation():
    status, _, _ = parse_enrollment_status(
        "Closed by Dept Ecology and Evolutionary Biology (0 capacity, 0 enrolled, 0 waitlisted)"
    )
    assert status is EnrollmentStatus.CLOSED


@pytest.mark.parametrize(
    ("raw", "status", "count", "capacity"),
    [
        ("0 of 40 Taken", WaitlistStatus.OPEN, 0, 40),
        ("12 of 30 Taken", WaitlistStatus.OPEN, 12, 30),
        ("No Waitlist", WaitlistStatus.NONE, 0, 0),
        ("Waitlist Full (2)", WaitlistStatus.FULL, 2, 2),
        (
            "5 Waitlisted, Contact Instructor/Department",
            WaitlistStatus.CONTACT_INSTRUCTOR,
            5,
            0,
        ),
        ("", WaitlistStatus.UNKNOWN, 0, 0),
    ],
)
def test_parse_waitlist_status(raw, status, count, capacity):
    assert parse_waitlist_status(raw) == (status, count, capacity)


@pytest.mark.parametrize(
    ("label", "expected"),
    [("Lec 1", ("Lec", 1)), ("Dis 1A", ("Dis", 1)), ("Lab 2B", ("Lab", 2)), ("Sem 3", ("Sem", 3))],
)
def test_parse_section_label(label, expected):
    assert parse_section_label(label) == expected


# --------------------------------------------------------------------------
# Section rows
# --------------------------------------------------------------------------


def test_parse_course_summary_real_response(fixture_text):
    sections = parse_course_summary(fixture_text("summary_comsci_32.html"), "26F", "COM SCI", "32")
    assert len(sections) == 1
    section = sections[0]

    assert section.registrar_id == "187096200"
    assert section.term_code == "26F"
    assert section.section_label == "Lec 1"
    assert section.format == "Lec"
    assert section.index == 1
    assert section.units == "4.0"
    assert section.instructors == ("Huang, B.K.",)
    assert section.enrollment.enrollment_status is EnrollmentStatus.OPEN
    assert section.enrollment.enrollment_count == 108
    assert section.enrollment.enrollment_capacity == 232
    assert section.enrollment.spots_left == 124
    assert section.enrollment.waitlist_status is WaitlistStatus.OPEN
    assert section.enrollment.waitlist_capacity == 30


def test_multi_valued_cells_split_on_br(fixture_text):
    """A section meeting twice a week packs both patterns into one cell."""
    section = parse_course_summary(fixture_text("summary_comsci_32.html"), "26F", "COM SCI", "32")[
        0
    ]
    assert section.days == ("T", "R")
    # <wbr/> hints inside times must not survive into the parsed value.
    assert section.times == ("4pm-5:50pm", "4pm-5:50pm")
    assert section.locations == ("Young Hall CS76", "Young Hall CS50")


def test_parse_course_summary_handles_course_not_offered(fixture_text):
    """The registrar serves an error div, not an empty body, for these."""
    assert (
        parse_course_summary(fixture_text("summary_math_151ah.html"), "26F", "MATH", "151AH") == []
    )


def test_parse_course_summary_letter_catalog_number(fixture_text):
    """AERO ST A -- a course whose 'number' has no digits at all."""
    sections = parse_course_summary(fixture_text("summary_aerost_a.html"), "26F", "AERO ST", "A")
    assert sections, "AERO ST A should have sections"
    assert all(s.registrar_id.isdigit() for s in sections)


def test_section_detail_url_is_wellformed(fixture_text):
    section = parse_course_summary(fixture_text("summary_comsci_32.html"), "26F", "COM SCI", "32")[
        0
    ]
    url = section.detail_url
    assert url.startswith("https://sa.ucla.edu/ro/Public/SOC/Results/ClassDetail?")
    assert "class_id=187096200" in url
    assert "term_cd=26F" in url


# --------------------------------------------------------------------------
# Detail tooltip
# --------------------------------------------------------------------------


def test_parse_section_details(fixture_text):
    details = parse_section_details(fixture_text("detail_tooltip_comsci_32.html"))
    # This section's webpage is literally "N/A"; we must not record that.
    assert "website" not in details


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_parse_terms(fixture_text):
    terms = parse_terms(fixture_text("term_select.html"))
    codes = [t.code for t in terms]
    assert "26F" in codes
    assert "261" in codes  # summer sessions
    by_code = {t.code: t for t in terms}
    assert by_code["26F"].name == "Fall 2026"
    assert by_code["26F"].is_summer is False
    assert by_code["261"].is_summer is True
    assert by_code["26F"].year == 2026


def test_parse_subject_areas(fixture_text):
    areas = parse_subject_areas(fixture_text("subject_areas.html"))
    by_code = {a.code: a for a in areas}
    assert len(areas) > 100
    assert by_code["AERO ST"].name == "Aerospace Studies"
    assert "COM SCI" in by_code
    # Codes with an ampersand must survive the HTML unescaping intact.
    assert any("&" in code for code in by_code)


def test_parse_course_titles(fixture_text):
    courses = parse_course_titles(fixture_text("course_titles_comsci_p1.html"), "COM SCI")
    by_number = {c.number: c for c in courses}
    assert by_number["1"].title == "Freshman Computer Science Seminar"
    assert by_number["32"].title == "Introduction to Computer Science II"
    assert by_number["32"].short_title == "COM SCI 32"
    # Ordinary courses get the wildcard index; no per-index fan-out needed.
    assert by_number["32"].section_indices == ("%",)


def test_has_more_pages(fixture_text):
    assert has_more_pages(fixture_text("course_titles_comsci_p1.html")) is True
    assert has_more_pages("") is False
    assert has_more_pages("   \n ") is False


# --------------------------------------------------------------------------
# Summer sessions
# --------------------------------------------------------------------------


def test_summer_sections_carry_their_session(fixture_text):
    """Summer responses interleave a session header before the rows it owns."""
    sections = parse_course_summary(
        fixture_text("summary_comsci_31_summer.html"), "261", "COM SCI", "31"
    )
    assert len(sections) == 1
    section = sections[0]
    assert section.summer_session == "A8"
    assert section.summer_duration_weeks == 8
    # The ordinary row parse still applies.
    assert section.enrollment.enrollment_status is EnrollmentStatus.OPEN
    assert section.locations == ("Online - Asynchronous",)


def test_non_summer_sections_have_no_session(fixture_text):
    section = parse_course_summary(fixture_text("summary_comsci_32.html"), "26F", "COM SCI", "32")[
        0
    ]
    assert section.summer_session is None
    assert section.summer_duration_weeks is None


def test_parse_terms_preserves_registrar_ordering(fixture_text):
    """Regression: term codes cannot be sorted chronologically as strings.

    Alphabetically ``26F < 26S < 26W``, but the year runs Winter, Spring, Fall.
    The registrar's dropdown order is the only reliable one, so it is recorded
    as ``position`` and must never be recomputed from the code.
    """
    terms = parse_terms(fixture_text("term_select.html"))
    codes = [t.code for t in terms]

    assert [t.position for t in terms] == list(range(len(terms)))
    assert codes == sorted(codes, key=lambda c: [t.code for t in terms].index(c))
    # The sorted-by-code order is genuinely different -- that is the bug.
    assert codes != sorted(codes, reverse=True)


def test_parse_terms_marks_the_registrar_selected_term(fixture_text):
    terms = parse_terms(fixture_text("term_select.html"))
    current = [t.code for t in terms if t.is_current]
    assert current == ["26F"], "exactly the option the registrar marked selected"
    # Sorting by code descending would have picked Winter 2027 instead.
    assert sorted((t.code for t in terms), reverse=True)[0] == "27W"
