"""Adaptive polling policy.

The cadence rules are the answer to "how do we poll without hammering UCLA",
so they get tested directly rather than inferred from scheduler behaviour.
"""

from __future__ import annotations

import datetime as dt

import pytest

from bruinwatch.services.scheduler import (
    ACTIVE_INTERVAL,
    BURST_INTERVAL,
    CAMPUS_TZ,
    IDLE_INTERVAL,
    choose_interval,
)


def at(hour: int) -> dt.datetime:
    return dt.datetime(2026, 8, 19, hour, 0, tzinfo=CAMPUS_TZ)


def test_enrollment_window_bursts():
    """Sub-minute polling only when a student could act on the result."""
    assert (
        choose_interval(
            now=at(10), in_enrollment_window=True, circuit_open=False, has_watchers=True
        )
        == BURST_INTERVAL
    )


@pytest.mark.parametrize("hour", [7, 12, 22])
def test_campus_daytime_polls_at_the_active_rate(hour):
    assert (
        choose_interval(
            now=at(hour), in_enrollment_window=False, circuit_open=False, has_watchers=True
        )
        == ACTIVE_INTERVAL
    )


@pytest.mark.parametrize("hour", [0, 3, 6, 23])
def test_overnight_backs_off(hour):
    assert (
        choose_interval(
            now=at(hour), in_enrollment_window=False, circuit_open=False, has_watchers=True
        )
        == IDLE_INTERVAL
    )


def test_nobody_watching_means_idle():
    """No subscriptions, no reason to poll fast -- even at midday."""
    assert (
        choose_interval(
            now=at(12), in_enrollment_window=True, circuit_open=False, has_watchers=False
        )
        == IDLE_INTERVAL
    )


def test_open_circuit_overrides_everything():
    """A tripped breaker must not be undone by an enrollment window."""
    assert (
        choose_interval(now=at(12), in_enrollment_window=True, circuit_open=True, has_watchers=True)
        == IDLE_INTERVAL
    )


def test_burst_is_faster_than_active_is_faster_than_idle():
    assert BURST_INTERVAL < ACTIVE_INTERVAL < IDLE_INTERVAL
    # Sanity-check the actual numbers, since these decide our request volume.
    assert dt.timedelta(seconds=30) == BURST_INTERVAL
    assert dt.timedelta(minutes=2) == ACTIVE_INTERVAL
    assert dt.timedelta(minutes=15) == IDLE_INTERVAL
