"""A single, polite HTTP client for the UCLA Schedule of Classes.

Every request in the process goes through one ``httpx.AsyncClient`` so that
connections are pooled and a global semaphore can cap how hard we lean on
sa.ucla.edu. The previous implementation issued bare, unbounded, *synchronous*
``requests.get`` calls from inside coroutines, which both blocked the Discord
event loop and scaled request volume with the number of subscribers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import urllib.parse
from types import TracebackType
from typing import TYPE_CHECKING, Self

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .model import FILTER_FLAGS

if TYPE_CHECKING:
    from tenacity import RetryCallState

log = structlog.get_logger(__name__)

#: The registrar's AJAX endpoints 404 without this header.
AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


class RegistrarError(RuntimeError):
    """A request to the registrar failed after exhausting retries."""


class RegistrarBlocked(RuntimeError):
    """The registrar is consistently refusing us; stop rather than continue.

    Deliberately *not* a :class:`RegistrarError`, so the per-course handlers
    that swallow individual failures let this one through and abort the run.
    """


class RegistrarRateLimited(RegistrarError):
    """The registrar asked us to slow down (429, or 503 with Retry-After).

    Retryable, unlike other 4xx: it says "not now", not "never".
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


#: Failures worth trying again: the network wobbled, the server is unwell, or
#: it explicitly asked us to wait. Other 4xx mean the request itself is wrong.
RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError, RegistrarRateLimited)

#: Ceiling on an honoured Retry-After, so a hostile or mistaken header cannot
#: park the scraper for hours.
MAX_RETRY_AFTER = 300.0

#: Slowest we will ever go after being throttled: one request every 2s.
MIN_RATE_INTERVAL_CEILING = 2.0

#: Status codes that mean "you are going too fast". 429 is the standard one;
#: **529 is what sa.ucla.edu actually returns**, along with a redirect to
#: /Error/TooManyRequests. 529 is non-standard and would otherwise be treated
#: as a generic server error, which retries without ever slowing down.
THROTTLE_STATUS = frozenset({408, 429, 529})

#: The registrar redirects throttled requests here, so the final URL is a
#: signal in its own right even if the status code changes.
THROTTLE_URL_MARKER = "/Error/TooManyRequests"

#: Consecutive throttles before we conclude we are blocked and stop. Without
#: this a rate-limited run grinds through the whole catalog failing every
#: request, producing a holed dataset and a lot of pointless load.
BLOCKED_AFTER_CONSECUTIVE = 8


def parse_retry_after(value: str | None) -> float | None:
    """Read a ``Retry-After`` header. Seconds, or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        delta = parsedate_to_datetime(value) - dt.datetime.now(dt.UTC)
        return max(0.0, delta.total_seconds())
    except (TypeError, ValueError):
        return None


class RegistrarClient:
    """Async HTTP access to the Schedule of Classes.

    Use as an async context manager, or call :meth:`aclose` when finished.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        max_concurrency: int = 10,
        timeout: float = 20.0,
        max_retries: int = 3,
        retry_initial_wait: float = 1.0,
        requests_per_second: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._retry_initial_wait = retry_initial_wait
        # A hard ceiling on request rate, independent of concurrency. The
        # semaphore bounds how many requests are in flight; this bounds how
        # often they start, which is what politeness actually means for a
        # long-running backfill against someone else's server.
        self._min_interval = 1.0 / requests_per_second if requests_per_second else 0.0
        self._rate_lock = asyncio.Lock()
        self._next_slot = 0.0
        self._consecutive_throttles = 0
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"},
            limits=httpx.Limits(
                max_connections=max_concurrency,
                max_keepalive_connections=max_concurrency,
            ),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _wait(self, retry_state: RetryCallState) -> float:
        """Backoff schedule: the server's Retry-After if it gave one, else
        exponential with jitter."""
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, RegistrarRateLimited) and exc.retry_after is not None:
            return min(exc.retry_after, MAX_RETRY_AFTER)
        return float(wait_exponential_jitter(initial=self._retry_initial_wait, max=30)(retry_state))

    def _on_throttled(self) -> None:
        """Slow down, and give up entirely if it keeps happening."""
        self._consecutive_throttles += 1
        self._throttle_harder()
        if self._consecutive_throttles >= BLOCKED_AFTER_CONSECUTIVE:
            log.error(
                "registrar_blocking_us",
                consecutive=self._consecutive_throttles,
                interval_s=round(self._min_interval, 3),
            )
            raise RegistrarBlocked(
                f"the registrar refused {self._consecutive_throttles} consecutive requests; "
                f"stopping. Re-run later, and lower --rate."
            )

    def _throttle_harder(self) -> None:
        """Halve our request rate after being told to slow down.

        Deliberately one-way for the life of the client: a long batch finishing
        slower is strictly better than one that gets blocked. If no rate limit
        was configured, start from one request per second rather than
        continuing unbounded.
        """
        previous = self._min_interval
        self._min_interval = min(max(self._min_interval * 2, 1.0), MIN_RATE_INTERVAL_CEILING)
        if self._min_interval != previous:
            log.warning(
                "rate_limited_backing_off",
                previous_interval_s=round(previous, 3),
                new_interval_s=round(self._min_interval, 3),
            )

    async def _await_rate_slot(self) -> None:
        """Block until this request is allowed to start.

        Hands out evenly spaced departure slots, so N concurrent callers still
        produce at most ``requests_per_second`` in aggregate rather than N
        bursts. No-op when no rate limit was configured.
        """
        if not self._min_interval:
            return
        loop = asyncio.get_running_loop()
        async with self._rate_lock:
            now = loop.time()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._min_interval
        delay = slot - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

    async def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        ajax: bool = True,
        raw_query: str | None = None,
    ) -> str:
        """GET a registrar URL and return the body, retrying transient failures.

        ``raw_query`` bypasses urlencode for the handful of endpoints that
        insist on their own escaping of spaces in the query string.
        """
        headers = AJAX_HEADERS if ajax else {}
        target = f"{url}?{raw_query}" if raw_query is not None else url

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=self._wait,
            retry=retry_if_exception_type(RETRYABLE),
            reraise=True,
        ):
            with attempt:
                await self._await_rate_slot()
                async with self._semaphore:
                    response = await self._client.get(
                        target,
                        params=params if raw_query is None else None,
                        headers=headers,
                    )
                retry_after = parse_retry_after(response.headers.get("Retry-After"))

                # "Slow down" is not "never": retryable, and it permanently
                # eases this client's rate for the rest of the run.
                throttled = response.status_code in THROTTLE_STATUS or (
                    THROTTLE_URL_MARKER in str(response.url)
                )
                if throttled:
                    self._on_throttled()
                    raise RegistrarRateLimited(
                        f"{response.status_code} from {response.url}", retry_after
                    )
                self._consecutive_throttles = 0
                if response.status_code >= 500:
                    if retry_after is not None:
                        raise RegistrarRateLimited(
                            f"{response.status_code} from {response.url}", retry_after
                        )
                    response.raise_for_status()
                # Other 4xx are permanent (bad model, unknown course).
                if response.status_code >= 400:
                    raise RegistrarError(f"{response.status_code} from {response.url}")
                return response.text

        raise RegistrarError(f"exhausted retries for {target}")  # pragma: no cover

    async def get_course_summary(self, model: str) -> str:
        """Fetch the sections table for a course."""
        return await self.get(
            _url("GetCourseSummary"),
            {"model": model, "FilterFlags": FILTER_FLAGS, "_": "1"},
        )

    async def get_course_titles(self, model: str, page_number: int) -> str:
        """Fetch one page of a subject area's course list."""
        return await self.get(
            _url("CourseTitlesView"),
            {
                "model": model,
                "search_by": "subject",
                "filterFlags": FILTER_FLAGS,
                "pageNumber": str(page_number),
            },
        )

    async def get_class_detail_tooltip(
        self,
        term: str,
        subject_area_code: str,
        catalog_number: str,
        registrar_id: str,
        index: int,
    ) -> str:
        """Fetch the popover with website, requisites and final-exam times.

        This endpoint rejects a normally-encoded query string, so the parameters
        are assembled by hand with ``%20`` for every space.
        """
        raw_query = "&".join(
            [
                f"term_cd={term}",
                f"subj_area_cd={urllib.parse.quote(subject_area_code)}",
                f"crs_catlg_no={catalog_number}",
                f"class_id={registrar_id}",
                f"class_no= {index:03d}  ",
            ]
        ).replace(" ", "%20")
        return await self.get(_url("ClassDetailTooltip"), raw_query=raw_query)

    async def get_soc_home(self) -> str:
        """Fetch the SOC landing page, which carries the term dropdown."""
        return await self.get("https://sa.ucla.edu/ro/Public/SOC/", ajax=False)

    async def get_results_page(self, term: str, subject_area_code: str) -> str:
        """Fetch a search-results page, which embeds the subject-area list."""
        return await self.get(
            _url(""),
            {"t": term, "sBy": "subject", "subj": subject_area_code},
            ajax=False,
        )


def _url(endpoint: str) -> str:
    base = "https://sa.ucla.edu/ro/Public/SOC/Results"
    return f"{base}/{endpoint}" if endpoint else base
