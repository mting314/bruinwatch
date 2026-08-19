"""HTTP client behaviour: retries, concurrency ceiling, error handling.

All network is stubbed with respx, so these run offline and fast.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from bruinwatch.registrar.client import RegistrarClient, RegistrarError
from bruinwatch.registrar.model import GET_COURSE_SUMMARY_URL


@pytest.fixture
def client():
    return RegistrarClient(
        user_agent="bruinwatch-test",
        max_concurrency=3,
        timeout=5.0,
        max_retries=2,
        # Keep the backoff out of the test runtime; the schedule itself is
        # tenacity's business, not ours.
        retry_initial_wait=0.001,
    )


@respx.mock
async def test_sends_ajax_header_and_user_agent(client):
    route = respx.get(GET_COURSE_SUMMARY_URL).mock(
        return_value=httpx.Response(200, text="<html></html>")
    )
    async with client:
        await client.get_course_summary('{"Term":"26F"}')

    request = route.calls.last.request
    # The registrar 404s AJAX endpoints without this header.
    assert request.headers["X-Requested-With"] == "XMLHttpRequest"
    assert request.headers["User-Agent"] == "bruinwatch-test"


@respx.mock
async def test_retries_server_errors_then_succeeds(client):
    route = respx.get(GET_COURSE_SUMMARY_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(500),
            httpx.Response(200, text="ok"),
        ]
    )
    async with client:
        assert await client.get_course_summary("{}") == "ok"
    assert route.call_count == 3


@respx.mock
async def test_gives_up_after_max_retries(client):
    route = respx.get(GET_COURSE_SUMMARY_URL).mock(return_value=httpx.Response(502))
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_course_summary("{}")
    # 1 initial attempt + 2 retries.
    assert route.call_count == 3


@respx.mock
async def test_client_errors_are_not_retried(client):
    """A 404 means a bad model, not a blip; retrying just wastes requests."""
    route = respx.get(GET_COURSE_SUMMARY_URL).mock(return_value=httpx.Response(404))
    async with client:
        with pytest.raises(RegistrarError):
            await client.get_course_summary("{}")
    assert route.call_count == 1


@respx.mock
async def test_retries_transport_errors(client):
    route = respx.get(GET_COURSE_SUMMARY_URL).mock(
        side_effect=[httpx.ConnectTimeout("boom"), httpx.Response(200, text="ok")]
    )
    async with client:
        assert await client.get_course_summary("{}") == "ok"
    assert route.call_count == 2


@respx.mock
async def test_concurrency_is_capped():
    """The semaphore must actually bound in-flight requests to the registrar."""
    in_flight = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200, text="ok")

    respx.get(GET_COURSE_SUMMARY_URL).mock(side_effect=handler)

    client = RegistrarClient(user_agent="t", max_concurrency=3, timeout=5.0, max_retries=0)
    async with client:
        await asyncio.gather(*(client.get_course_summary("{}") for _ in range(12)))

    assert peak <= 3, f"expected at most 3 concurrent requests, saw {peak}"


@respx.mock
async def test_detail_tooltip_uses_percent_encoded_spaces(client):
    """This endpoint rejects a normally urlencoded query string."""
    route = respx.get(
        url__startswith="https://sa.ucla.edu/ro/Public/SOC/Results/ClassDetailTooltip"
    ).mock(return_value=httpx.Response(200, text=""))
    async with client:
        await client.get_class_detail_tooltip(
            term="26F",
            subject_area_code="COM SCI",
            catalog_number="0032    ",
            registrar_id="187096200",
            index=1,
        )

    url = str(route.calls.last.request.url)
    assert "+" not in url.split("?", 1)[1], "spaces must be %20, never +"
    assert "class_id=187096200" in url
    assert "class_no=%20001%20%20" in url
