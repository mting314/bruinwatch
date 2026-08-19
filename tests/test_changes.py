"""Change-detection rules.

These encode the single most important behaviour in the bot: when a scrape is
worth waking somebody up for. They run with no database and no network.
"""

from __future__ import annotations

import pytest

from bruinwatch.registrar.types import EnrollmentNumbers, EnrollmentStatus, WaitlistStatus
from bruinwatch.services.changes import (
    Reason,
    classify,
    crosses_spot_threshold,
    notification_reason,
)


def numbers(
    status: EnrollmentStatus = EnrollmentStatus.OPEN,
    count: int = 100,
    capacity: int = 200,
    waitlist: WaitlistStatus = WaitlistStatus.OPEN,
    wl_count: int = 0,
    wl_capacity: int = 30,
) -> EnrollmentNumbers:
    return EnrollmentNumbers(
        enrollment_status=status,
        enrollment_count=count,
        enrollment_capacity=capacity,
        waitlist_status=waitlist,
        waitlist_count=wl_count,
        waitlist_capacity=wl_capacity,
    )


# -- classify() ------------------------------------------------------------


def test_new_section_seeds_history_but_notifies_nobody():
    """Otherwise the first sweep after adding a course DMs everyone."""
    decision = classify(None, numbers())
    assert decision.is_new is True
    assert decision.record_history is True
    assert decision.notify is False


def test_identical_observation_does_nothing():
    """The common case: most polls see no change at all."""
    current = numbers()
    decision = classify(current, current)
    assert decision.record_history is False
    assert decision.notify is False


def test_count_change_records_history_but_does_not_notify():
    """108/232 -> 109/232 is a data point, not an event."""
    decision = classify(numbers(count=108), numbers(count=109))
    assert decision.record_history is True
    assert decision.notify is False


def test_status_change_records_history_and_notifies():
    decision = classify(
        numbers(status=EnrollmentStatus.OPEN),
        numbers(status=EnrollmentStatus.FULL),
    )
    assert decision.record_history is True
    assert decision.notify_reason is Reason.STATUS_CHANGE


def test_waitlist_only_movement_records_history():
    """The waitlist is part of the series even though it isn't notify-worthy."""
    decision = classify(numbers(wl_count=0), numbers(wl_count=3))
    assert decision.record_history is True
    assert decision.notify is False


def test_record_history_can_be_disabled_without_affecting_notifications():
    decision = classify(
        numbers(status=EnrollmentStatus.FULL),
        numbers(status=EnrollmentStatus.OPEN),
        record_history=False,
    )
    assert decision.record_history is False
    assert decision.notify_reason is Reason.STATUS_CHANGE


# -- spots threshold -------------------------------------------------------


def test_spot_threshold_is_edge_triggered():
    """Fires on the crossing, then stays quiet while still below."""
    above = numbers(count=190, capacity=200)  # 10 left
    crossing = numbers(count=197, capacity=200)  # 3 left
    still_below = numbers(count=198, capacity=200)  # 2 left

    assert crosses_spot_threshold(above, crossing, 5) is True
    assert crosses_spot_threshold(crossing, still_below, 5) is False


def test_spot_threshold_ignores_non_open_sections():
    full_before = numbers(status=EnrollmentStatus.FULL, count=200, capacity=200)
    full_after = numbers(status=EnrollmentStatus.FULL, count=201, capacity=200)
    assert crosses_spot_threshold(full_before, full_after, 5) is False


def test_spot_threshold_disabled_when_unset():
    assert crosses_spot_threshold(numbers(count=190), numbers(count=199), None) is False


@pytest.mark.parametrize("threshold", [0, 1, 5, 50])
def test_spot_threshold_boundaries(threshold):
    capacity = 200
    before = numbers(count=capacity - threshold - 1, capacity=capacity)
    after = numbers(count=capacity - threshold, capacity=capacity)
    assert crosses_spot_threshold(before, after, threshold) is True


# -- per-subscriber reason -------------------------------------------------


def test_status_change_beats_threshold():
    """A subscriber with a threshold set still gets one DM, not two."""
    reason = notification_reason(
        numbers(status=EnrollmentStatus.OPEN, count=190),
        numbers(status=EnrollmentStatus.FULL, count=200),
        threshold=5,
    )
    assert reason is Reason.STATUS_CHANGE


def test_threshold_only_for_subscriber_who_set_it():
    previous = numbers(count=190, capacity=200)
    current = numbers(count=197, capacity=200)
    assert notification_reason(previous, current, threshold=5) is Reason.SPOTS_THRESHOLD
    assert notification_reason(previous, current, threshold=None) is None


def test_reopening_notifies():
    """The event people actually care about."""
    reason = notification_reason(
        numbers(status=EnrollmentStatus.FULL, count=200, capacity=200),
        numbers(status=EnrollmentStatus.OPEN, count=199, capacity=200),
        threshold=None,
    )
    assert reason is Reason.STATUS_CHANGE
