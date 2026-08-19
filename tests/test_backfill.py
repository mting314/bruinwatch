"""Backfill term maths, cost estimation and rate limiting.

The database-backed resume behaviour lives in ``test_backfill_db.py``.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from bruinwatch.registrar.client import RegistrarClient
from bruinwatch.registrar.model import GET_COURSE_SUMMARY_URL
from bruinwatch.services import backfill

# -- term expansion --------------------------------------------------------


def test_expand_terms_covers_a_year_in_order():
    assert backfill.expand_terms("26W", "26F") == ["26F", "262", "261", "26S", "26W"]


def test_expand_terms_spans_years_newest_first():
    terms = backfill.expand_terms("24F", "26W")
    assert terms[0] == "26W"
    assert terms[-1] == "24F"
    assert "25S" in terms and "251" in terms
    # Strictly newest-to-oldest.
    from bruinwatch.registrar.types import term_position

    assert terms == sorted(terms, key=term_position)


def test_expand_terms_is_order_insensitive():
    assert backfill.expand_terms("23W", "24F") == backfill.expand_terms("24F", "23W")


def test_expand_terms_single_term():
    assert backfill.expand_terms("26F", "26F") == ["26F"]


def test_expand_terms_2023_onwards_is_the_expected_size():
    """The range actually requested: 2023 to the newest published term."""
    terms = backfill.expand_terms("23W", "27S")
    assert terms[0] == "27S"
    assert terms[-1] == "23W"
    # Five terms a year for 2023-2026, plus 27W and 27S.
    assert len(terms) == 22


# -- validation ------------------------------------------------------------


def test_validate_rejects_terms_before_the_archive():
    with pytest.raises(ValueError, match="99F"):
        backfill.validate_terms(["98F"])


def test_validate_rejects_malformed_codes():
    with pytest.raises(ValueError, match="malformed"):
        backfill.validate_terms(["nope"])


def test_validate_accepts_the_earliest_served_term():
    assert backfill.validate_terms(["99F"]) == ["99F"]


# -- naming ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "name"),
    [
        ("26F", "Fall 2026"),
        ("26W", "Winter 2026"),
        ("26S", "Spring 2026"),
        ("261", "Summer Sessions 2026"),
        ("99F", "Fall 1999"),
    ],
)
def test_term_name(code, name):
    assert backfill.term_name(code) == name


def test_term_end_dates_are_ordered_within_a_year():
    dates = [backfill.term_end_date(f"26{s}") for s in ("W", "S", "1", "2", "F")]
    assert dates == sorted(dates)


# -- cost estimate ---------------------------------------------------------


def test_estimate_scales_with_terms_and_rate():
    one = backfill.estimate(["26F"], 5.0)
    two = backfill.estimate(["26F", "26S"], 5.0)
    assert two["requests"] == 2 * one["requests"]
    assert two["hours"] == pytest.approx(2 * one["hours"])

    slower = backfill.estimate(["26F"], 1.0)
    assert slower["hours"] == pytest.approx(5 * one["hours"])


def test_estimate_of_the_requested_range_is_under_a_day():
    """2023 onwards at the default rate should be an overnight job."""
    cost = backfill.estimate(backfill.expand_terms("23W", "27S"), 5.0)
    assert cost["requests"] == pytest.approx(242_000, rel=0.01)
    assert 12 < cost["hours"] < 16


# -- rate limiting ---------------------------------------------------------


@respx.mock
async def test_client_honours_the_rate_limit():
    """The politeness budget must hold across concurrent callers, not per-caller."""
    respx.get(GET_COURSE_SUMMARY_URL).mock(return_value=httpx.Response(200, text="ok"))

    rate = 20.0  # 50ms apart
    client = RegistrarClient(
        user_agent="t", max_concurrency=8, max_retries=0, requests_per_second=rate
    )
    started = time.monotonic()
    async with client:
        await asyncio.gather(*(client.get_course_summary("{}") for _ in range(10)))
    elapsed = time.monotonic() - started

    # Ten requests at 20/s cannot finish faster than ~450ms however much
    # concurrency is available.
    assert elapsed >= 9 / rate * 0.9, f"finished in {elapsed:.2f}s, faster than the limit allows"


@respx.mock
async def test_no_rate_limit_by_default():
    """The live poller relies on the semaphore alone; it must not be throttled."""
    respx.get(GET_COURSE_SUMMARY_URL).mock(return_value=httpx.Response(200, text="ok"))
    client = RegistrarClient(user_agent="t", max_concurrency=8, max_retries=0)
    started = time.monotonic()
    async with client:
        await asyncio.gather(*(client.get_course_summary("{}") for _ in range(10)))
    assert time.monotonic() - started < 0.5
