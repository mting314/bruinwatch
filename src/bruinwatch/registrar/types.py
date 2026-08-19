"""Value types for the UCLA Schedule of Classes.

Everything here is a plain frozen dataclass with no I/O, no ORM and no Discord
imports, so the whole scraping layer can be unit tested against saved HTML.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum

TERM_RE = re.compile(r"^\d{2}([FWS]|[12])$")

#: Order of terms inside one calendar year. Term codes cannot be sorted as
#: strings -- alphabetically ``F < S < W``, but the year runs Winter, Spring,
#: the two Summer sessions, then Fall.
SEASON_ORDER = {"W": 0, "S": 1, "1": 2, "2": 3, "F": 4}

#: Arbitrary base larger than any real key, so newer terms get a smaller
#: ``position``. Keeps computed positions positive and comparable forever.
_POSITION_BASE = 100_000


#: Two-digit years at or above this belong to the 1900s. The registrar's
#: archive starts at 99F (Fall 1999), so the pivot only has to separate the
#: tail of the last century from this one.
_CENTURY_PIVOT = 90


def calendar_year(code: str) -> int:
    """Full year for a term code: ``26F`` -> 2026, ``99F`` -> 1999."""
    two_digit = int(code[:2])
    return (1900 if two_digit >= _CENTURY_PIVOT else 2000) + two_digit


def chronological_key(code: str) -> int:
    """A sortable integer for a term code. Larger means later in time."""
    return calendar_year(code) * 10 + SEASON_ORDER[code[-1]]


def term_position(code: str) -> int:
    """Sort position for a term: smaller is newer.

    Computed from the code rather than taken from the registrar's dropdown, so
    that backfilled terms -- which the dropdown never lists -- interleave
    correctly with live ones. The dropdown happens to be in exactly this order,
    which ``tests/test_parsing.py`` asserts.
    """
    return _POSITION_BASE - chronological_key(code)


class EnrollmentStatus(StrEnum):
    """Normalized enrollment status.

    The registrar renders a dozen different strings; we collapse them to these.
    Only a transition *between* these values is worth a notification.
    """

    OPEN = "Open"
    WAITLIST = "Waitlist"
    FULL = "Full"
    CLOSED = "Closed"
    TENTATIVE = "Tentative"
    CANCELLED = "Cancelled"
    UNKNOWN = "Unknown"

    @property
    def is_enrollable(self) -> bool:
        return self in (EnrollmentStatus.OPEN, EnrollmentStatus.WAITLIST)


class WaitlistStatus(StrEnum):
    OPEN = "Open"
    FULL = "Full"
    NONE = "None"
    CONTACT_INSTRUCTOR = "Contact instructor"
    UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True)
class Term:
    """A UCLA term code such as ``26F`` (Fall 2026) or ``261`` (Summer 2026)."""

    code: str
    name: str = ""
    #: Index in the registrar's dropdown, 0 = newest. The only reliable
    #: chronological ordering; the codes themselves do not sort correctly.
    position: int = 0
    #: The term the registrar has selected, i.e. the one enrollment is for.
    is_current: bool = False

    def __post_init__(self) -> None:
        if not TERM_RE.match(self.code):
            raise ValueError(f"malformed term code: {self.code!r}")

    @property
    def is_summer(self) -> bool:
        return self.code[-1].isdigit()

    @property
    def year(self) -> int:
        return calendar_year(self.code)


@dataclass(frozen=True, slots=True)
class SubjectArea:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class Course:
    """A course offering, keyed by subject area + catalog number."""

    subject_area_code: str
    number: str
    title: str = ""
    description: str | None = None
    units: str | None = None
    #: Section indices for courses with multiple independent listings. ``["%"]``
    #: is the wildcard meaning "all sections", which is the common case.
    section_indices: tuple[str, ...] = ("%",)

    @property
    def short_title(self) -> str:
        return f"{self.subject_area_code} {self.number}"


@dataclass(frozen=True, slots=True)
class EnrollmentNumbers:
    """The mutable enrollment state of a section.

    Equality on this type is what drives history recording: if any field moved
    we append a row to ``enrollment_data``. A change to ``enrollment_status``
    alone is what drives notifications.
    """

    enrollment_status: EnrollmentStatus = EnrollmentStatus.UNKNOWN
    enrollment_count: int = 0
    enrollment_capacity: int = 0
    waitlist_status: WaitlistStatus = WaitlistStatus.UNKNOWN
    waitlist_count: int = 0
    waitlist_capacity: int = 0

    @property
    def spots_left(self) -> int:
        return max(0, self.enrollment_capacity - self.enrollment_count)


@dataclass(frozen=True, slots=True)
class Section:
    """One scheduled section (Lec 1, Dis 1A, ...) of a course in a term."""

    registrar_id: str
    term_code: str
    subject_area_code: str
    course_number: str
    #: Human label, e.g. "Lec 1".
    section_label: str
    #: Numeric index parsed out of the label, e.g. 1 for "Lec 1".
    index: int
    format: str = ""
    days: tuple[str, ...] = ()
    times: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    instructors: tuple[str, ...] = ()
    units: str = ""
    enrollment: EnrollmentNumbers = field(default_factory=EnrollmentNumbers)
    website: str | None = None
    final_start: dt.datetime | None = None
    final_end: dt.datetime | None = None
    summer_session: str | None = None
    summer_duration_weeks: int | None = None

    @property
    def detail_url(self) -> str:
        from .model import class_detail_url

        return class_detail_url(
            term=self.term_code,
            subject_area_code=self.subject_area_code,
            course_number=self.course_number,
            registrar_id=self.registrar_id,
            index=self.index,
        )
