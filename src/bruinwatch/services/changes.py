"""The change-detection rules, as pure functions.

Deliberately separated from :mod:`bruinwatch.services.sync`, which owns the SQL.
These rules are the part worth reasoning about carefully -- whether a scrape
result is worth a database row, a DM, or nothing at all -- so they are kept
free of any I/O and tested directly.
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum

from ..registrar.types import EnrollmentNumbers, EnrollmentStatus


class Reason(StrEnum):
    STATUS_CHANGE = "status_change"
    SPOTS_THRESHOLD = "spots_threshold"


@dataclasses.dataclass(frozen=True, slots=True)
class ChangeDecision:
    """What to do about one observation of a section."""

    #: Append a row to ``enrollment_data``.
    record_history: bool = False
    #: Notify subscribers, and why. ``None`` means nobody is notified.
    notify_reason: Reason | None = None
    #: True the first time we ever see a section.
    is_new: bool = False

    @property
    def notify(self) -> bool:
        return self.notify_reason is not None


def classify(
    previous: EnrollmentNumbers | None,
    current: EnrollmentNumbers,
    *,
    record_history: bool = True,
) -> ChangeDecision:
    """Decide what a scrape result means.

    Three rules, in order:

    1. **A section we have never seen** seeds the history series but notifies
       nobody. Without this, the first sweep after adding a course would DM
       every subscriber about a "change" from nothing to its current state.
    2. **Any** numeric or status movement records history -- that is the data
       the enrollment chart is built from.
    3. Only a change of *enrollment status* notifies. A class going from 108/232
       to 109/232 is not news; going from Open to Full is.
    """
    if previous is None:
        return ChangeDecision(record_history=record_history, is_new=True)

    if previous == current:
        return ChangeDecision()

    reason = (
        Reason.STATUS_CHANGE if previous.enrollment_status != current.enrollment_status else None
    )
    return ChangeDecision(record_history=record_history, notify_reason=reason)


def crosses_spot_threshold(
    previous: EnrollmentNumbers,
    current: EnrollmentNumbers,
    threshold: int | None,
) -> bool:
    """Whether an open section just fell to or below a subscriber's threshold.

    Edge-triggered, not level-triggered: it must have been *above* the threshold
    before, otherwise every poll of a nearly-full class would fire again.
    """
    if threshold is None:
        return False
    if current.enrollment_status is not EnrollmentStatus.OPEN:
        return False
    return current.spots_left <= threshold < previous.spots_left


def notification_reason(
    previous: EnrollmentNumbers,
    current: EnrollmentNumbers,
    threshold: int | None,
) -> Reason | None:
    """The reason to notify one particular subscriber, if any."""
    if previous.enrollment_status != current.enrollment_status:
        return Reason.STATUS_CHANGE
    if crosses_spot_threshold(previous, current, threshold):
        return Reason.SPOTS_THRESHOLD
    return None
