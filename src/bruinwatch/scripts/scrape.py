"""Run one scraper tier and exit.

    uv run bruinwatch-scrape all-sections
    uv run bruinwatch-scrape catalog --term 26F

Built for cron and for Cloud Run Jobs. The bot normally runs these on an
in-process APScheduler, which needs a long-lived process; this is the same work
as a one-shot, so the scraper can be scheduled externally and the process can
exit. Without the Discord gateway there is nothing left that has to stay up.

Exits non-zero if the job raised, so a scheduler can alert on it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Literal, get_args

import structlog

from .. import logging as log_setup
from ..config import Settings
from ..db.session import create_engine, create_session_factory, wait_for_database
from ..registrar import RegistrarClient
from ..services.scheduler import ScraperService

Job = Literal["terms", "subject-areas", "catalog", "all-sections", "watched", "bootstrap"]
JOBS: tuple[str, ...] = get_args(Job)


def parse_shard(value: str | None) -> tuple[int, int] | None:
    """``"3/12"`` -> ``(3, 12)``. The index is taken modulo the count, so a
    monotonically increasing run number can be passed straight in."""
    if not value:
        return None
    index, _, count = value.partition("/")
    if not count.isdigit() or int(count) < 1 or not index.lstrip("-").isdigit():
        raise ValueError(f"expected INDEX/COUNT, got {value!r}")
    return int(index), int(count)


async def run(
    job: str,
    rate: float | None,
    concurrency: int,
    term_code: str | None = None,
    shard: tuple[int, int] | None = None,
) -> int:
    settings = Settings()
    log_setup.configure(settings.log_level, json_output=settings.log_json)
    log = structlog.get_logger("scrape")

    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        single_connection=settings.db_single_connection,
    )
    factory = create_session_factory(engine)
    await wait_for_database(engine)
    client = RegistrarClient(
        user_agent=settings.user_agent,
        max_concurrency=concurrency,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        requests_per_second=rate,
    )
    # No scheduler is started: this only borrows the service's job bodies.
    service = ScraperService(settings, factory, client)

    started = time.monotonic()
    try:
        if job == "bootstrap":
            from ..services.scheduler import bootstrap

            await bootstrap(client, factory)
        else:
            await service.run_now(job, term_code=term_code, shard=shard)
    except Exception:
        log.exception("scrape_failed", job=job)
        return 1
    finally:
        await client.aclose()
        await engine.dispose()

    log.info("scrape_complete", job=job, seconds=round(time.monotonic() - started, 1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="bruinwatch-scrape", description=__doc__)
    parser.add_argument("job", choices=JOBS, help="Which tier to run")
    parser.add_argument(
        "--rate",
        type=float,
        default=None,
        help="Requests per second ceiling. Unset means bounded only by --concurrency.",
    )
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument(
        "--term",
        default=None,
        help="Restrict to one term. Only meaningful for all-sections; the "
        "current term is usually the only one worth sweeping often.",
    )
    parser.add_argument(
        "--shard",
        default=None,
        metavar="INDEX/COUNT",
        help="Sweep only every COUNTth subject, offset by INDEX. The registrar "
        "refuses large sweeps, so successive runs take different slices and the "
        "union is complete over a day or two.",
    )
    args = parser.parse_args()

    settings_concurrency = args.concurrency
    if settings_concurrency is None:
        try:
            settings_concurrency = Settings().max_concurrency
        except Exception:
            settings_concurrency = 10
    try:
        shard = parse_shard(args.shard)
    except ValueError as exc:
        print(f"Bad --shard: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(run(args.job, args.rate, settings_concurrency, args.term, shard))


if __name__ == "__main__":
    raise SystemExit(main())
