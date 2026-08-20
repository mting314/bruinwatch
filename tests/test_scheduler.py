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


# -- sharding --------------------------------------------------------------


def test_shard_assignment_is_stable():
    """A subject must always land in the same shard, or successive runs would
    re-cover the same ground and never complete the union."""
    from bruinwatch.services.scheduler import _subject_shard

    for code in ("COM SCI", "MATH", "C&S BIO"):
        assert _subject_shard(code, 12) == _subject_shard(code, 12)


def test_shards_partition_the_subjects():
    """Every subject lands in exactly one shard, and the shards cover them all."""
    import pathlib

    from bruinwatch.registrar.parsing import parse_subject_areas
    from bruinwatch.services.scheduler import _subject_shard

    codes = [
        a.code
        for a in parse_subject_areas(
            (pathlib.Path(__file__).parent / "fixtures" / "subject_areas.html").read_text()
        )
    ]
    count = 12
    buckets: dict[int, list[str]] = {i: [] for i in range(count)}
    for code in codes:
        buckets[_subject_shard(code, count)].append(code)

    assert sum(len(v) for v in buckets.values()) == len(codes)
    assert not any(len(v) == 0 for v in buckets.values()), "an empty shard wastes a run"
    # Reasonably even, so no single run is dramatically heavier than the rest.
    largest, smallest = max(map(len, buckets.values())), min(map(len, buckets.values()))
    assert largest <= smallest * 3, f"lumpy shards: {largest} vs {smallest}"


def test_sharding_cuts_the_request_volume_as_expected():
    """The whole point: the registrar refused ~2,000 requests, and an
    unsharded three-term sweep is ~31,000."""
    import pathlib

    from bruinwatch.registrar.parsing import parse_subject_areas
    from bruinwatch.services.scheduler import _subject_shard

    codes = [
        a.code
        for a in parse_subject_areas(
            (pathlib.Path(__file__).parent / "fixtures" / "subject_areas.html").read_text()
        )
    ]
    courses_per_subject = 62
    count = 12
    per_run = (
        max(sum(1 for c in codes if _subject_shard(c, count) == i) for i in range(count))
        * courses_per_subject
    )
    assert per_run < 1500, f"worst shard is still {per_run} requests"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0/12", (0, 12)), ("47/12", (47, 12)), ("1/1", (1, 1)), (None, None), ("", None)],
)
def test_parse_shard(value, expected):
    from bruinwatch.scripts.scrape import parse_shard

    assert parse_shard(value) == expected


@pytest.mark.parametrize("bad", ["12", "a/b", "1/0", "1/x"])
def test_parse_shard_rejects_nonsense(bad):
    from bruinwatch.scripts.scrape import parse_shard

    with pytest.raises(ValueError):
        parse_shard(bad)
