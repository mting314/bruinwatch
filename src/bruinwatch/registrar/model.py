"""Deterministic construction of the registrar's ``model`` query parameter.

The Schedule of Classes drives its AJAX endpoints with an opaque-looking JSON
``model`` blob plus a base64 ``Token``. The obvious way to get one is to scrape
the ``Iwe_ClassSearch_SearchResults.AddToCourseData({...})`` script tag off a
search-results page -- which is what this bot used to do, caching every blob for
every course in the catalog just so it could later ask about enrollment.

It turns out the blob is a pure function of the subject area code and catalog
number, so we can build it ourselves and skip the whole cache. This is a port of
hotseat.io's ``CreateFormattedModel`` (lambdas/fetch-sections/fetching.go).

Worked example, ``COM SCI 32`` in ``26F``::

    number            "32"        -> lead="", num="32", trail=""
    catalog_number    "0032    "  = f"{num:0>4}{trail:<2}{lead:<2}"
    path              "COMSCI0032" = subject-without-punctuation + f"{num:0>4}" + trail + lead
    token             b64("0032    COMSCI0032") = "MDAzMiAgICBDT01TQ0kwMDMy"
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse

#: Catalog numbers look like ``32``, ``M151B``, ``CM122``, ``A``. The leading
#: characters are the multi-listed/honors prefixes (C, M), the trailing ones are
#: the suffix letters (A, B, AH...).
COURSE_NUMBER_RE = re.compile(r"^([CM]*)(\d*)([A-Z]*)$")

#: Course numbers in the 300s and 500s are professional-school/teaching-practicum
#: listings that nobody watches for enrollment. Skipped except for the
#: professional schools where they are the entire catalog.
IGNORED_COURSE_NUMBER_RE = re.compile(r"^[35]\d{2}")
PROFESSIONAL_SUBJECT_AREAS = frozenset({"LAW", "DENT", "MED"})

BASE_URL = "https://sa.ucla.edu/ro/Public/SOC"
SOC_HOME_URL = f"{BASE_URL}/"
RESULTS_URL = f"{BASE_URL}/Results"
COURSE_TITLES_VIEW_URL = f"{BASE_URL}/Results/CourseTitlesView"
GET_COURSE_SUMMARY_URL = f"{BASE_URL}/Results/GetCourseSummary"
CLASS_DETAIL_TOOLTIP_URL = f"{BASE_URL}/Results/ClassDetailTooltip"
CLASS_DETAIL_URL = f"{BASE_URL}/Results/ClassDetail"

#: Deliberately permissive: we want every section back and filter in Python.
#: Matches hotseat.io's FilterFlags, which is wider than the registrar's own
#: default (it includes weekends and a 2am-11pm window).
FILTER_FLAGS = (
    '{"enrollment_status":"O,W,C,X,T,S","advanced":"y","meet_days":"M,T,W,R,F,S,U",'
    '"start_time":"2:00 am","end_time":"11:00 pm","meet_locations":null,"meet_units":null,'
    '"instructor":null,"class_career":null,"impacted":null,"enrollment_restrictions":null,'
    '"enforced_requisites":null,"individual_studies":null,"summer_session":null}'
)


def split_course_number(number: str) -> tuple[str, str, str]:
    """Split ``M151B`` into ``("M", "151", "B")``.

    Falls back to treating the whole string as trailing characters for oddities
    like ``AERO ST A`` where the catalog "number" has no digits at all.
    """
    match = COURSE_NUMBER_RE.match(number.strip().upper())
    if match is None:
        return "", "", number.strip().upper()
    return match.group(1), match.group(2), match.group(3)


def catalog_number(number: str) -> str:
    """Build the registrar's fixed-width catalog number, e.g. ``"0032    "``."""
    lead, num, trail = split_course_number(number)
    return f"{num:0>4}{trail:<2}{lead:<2}"


def course_path(subject_area_code: str, number: str) -> str:
    """Build the registrar's course ``Path``, e.g. ``"COMSCI0032"``."""
    lead, num, trail = split_course_number(number)
    compact_subject = subject_area_code.upper().replace("&", "").replace(" ", "")
    return f"{compact_subject}{num:0>4}{trail}{lead}"


def build_model(
    subject_area_code: str,
    number: str,
    term: str,
    class_number: str = "%",
) -> str:
    """Return the JSON ``model`` parameter for a course summary request.

    ``class_number`` is the section index; ``"%"`` (the default) means "every
    section", which is what we want for all but the handful of courses the
    registrar lists multiple times under distinct indices.
    """
    catalog = catalog_number(number)
    path = course_path(subject_area_code, number)
    token = base64.b64encode(f"{catalog}{path}".encode()).decode()
    lead, _, _ = split_course_number(number)

    # Key order and the trailing-space padding both matter; the endpoint 404s or
    # silently returns nothing if the blob does not look like its own output.
    model = {
        "Term": term,
        "SubjectAreaCode": f"{subject_area_code.upper():<7}",
        "CatalogNumber": catalog,
        "IsRoot": True,
        "SessionGroup": "%",
        "ClassNumber": class_number,
        "SequenceNumber": None,
        "Path": path,
        # "M" in the catalog number marks a multiple-listed course (the same
        # class offered under several departments).
        "MultiListedClassFlag": "y" if "M" in lead else "n",
        "Token": token,
    }
    return json.dumps(model, separators=(",", ":"))


def build_subject_model(subject_area_code: str, term: str) -> str:
    """Return the ``model`` parameter used to page through a subject's courses."""
    model = {
        "subj_area_cd": subject_area_code.upper(),
        "search_by": "subject",
        "term_cd": term,
        "SubjectAreaName": "",
        "CrsCatlgName": "Enter a Catalog Number or Class Title (Optional)",
        "ActiveEnrollmentFlag": "n",
        "HasData": "True",
    }
    return json.dumps(model, separators=(",", ":"))


def is_interesting_course(subject_area_code: str, number: str) -> bool:
    """Whether a course is worth tracking enrollment for."""
    if subject_area_code.upper() in PROFESSIONAL_SUBJECT_AREAS:
        return True
    return not IGNORED_COURSE_NUMBER_RE.match(number.strip().upper())


def class_detail_url(
    term: str,
    subject_area_code: str,
    course_number: str,
    registrar_id: str,
    index: int,
) -> str:
    """Public, user-facing URL for a single section's detail page."""
    query = urllib.parse.urlencode(
        {
            "term_cd": term,
            "subj_area_cd": subject_area_code.upper(),
            "crs_catlg_no": catalog_number(course_number),
            "class_id": registrar_id,
            "class_no": f" {index:03d}  ",
        }
    )
    return f"{CLASS_DETAIL_URL}?{query}"


def public_results_url(term: str, registrar_id: str) -> str:
    """Public search-results URL that resolves a bare class ID."""
    query = urllib.parse.urlencode({"t": term, "sBy": "classidnumber", "id": registrar_id})
    return f"{RESULTS_URL}?{query}"
