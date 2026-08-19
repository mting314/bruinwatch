"""Backfill the catalog for past terms.

    uv run bruinwatch-backfill --from 23W --to 26F
    uv run bruinwatch-backfill --from 23W --to 26F --rate 2 --dry-run

Resumable: each (term, subject) unit is recorded on completion, so an
interrupted run picks up where it stopped. Re-running is safe.

Records what was offered and how full it ended up. It does **not** recover
enrollment history -- the archive holds one frozen snapshot per section, not a
time series. See :mod:`bruinwatch.services.backfill`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time

import structlog

from .. import logging as log_setup
from ..config import Settings
from ..db.session import create_engine, create_session_factory
from ..registrar import RegistrarClient
from ..services import backfill as engine


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    log_setup.configure(settings.log_level, json_output=settings.log_json)
    log = structlog.get_logger("backfill")

    try:
        terms = engine.validate_terms(engine.expand_terms(args.start, args.end))
    except (ValueError, KeyError) as exc:
        print(f"Bad term range: {exc}", file=sys.stderr)
        return 2

    cost = engine.estimate(terms, args.rate)
    print(f"Terms ({len(terms)}): {', '.join(terms)}")
    print(
        f"Estimated ~{cost['requests']:,.0f} requests at {args.rate}/s "
        f"-> ~{cost['hours']:.1f} h, ~{cost['gigabytes']:.1f} GB downloaded."
    )
    print(
        "This records the catalog and each section's final state. It cannot\n"
        "recover enrollment-over-time: the archive holds one frozen snapshot."
    )
    if args.dry_run:
        print("\n--dry-run: stopping before any request.")
        return 0

    engine_db = create_engine(settings.database_url)
    factory = create_session_factory(engine_db)
    client = RegistrarClient(
        user_agent=settings.user_agent,
        max_concurrency=args.concurrency,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        requests_per_second=args.rate,
    )

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    started = time.monotonic()
    done_units = 0
    # Only an order-of-magnitude guide: subjects vary hugely in size, and
    # resumed units complete instantly.
    total_units = len(terms) * 168

    async def on_progress(term: str, subject: str, sections: int) -> None:
        nonlocal done_units
        done_units += 1
        elapsed = time.monotonic() - started
        rate = done_units / elapsed if elapsed else 0
        remaining = (total_units - done_units) / rate / 60 if rate else 0
        print(
            f"  [{elapsed / 60:6.1f}m] {term} {subject:<10} {sections:5d} sections"
            f"   ~{remaining:.0f}m left",
            flush=True,
        )
        if stopping.is_set():
            raise KeyboardInterrupt

    print()
    try:
        result = await engine.backfill(
            client, factory, terms, resume=not args.no_resume, on_progress=on_progress
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run the same command to resume.", file=sys.stderr)
        return 130
    except Exception:
        log.exception("backfill_failed")
        print("\nFailed. Re-run the same command to resume.", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
        await engine_db.dispose()

    minutes = (time.monotonic() - started) / 60
    print(
        f"\nDone in {minutes:.1f} min: {result.terms} terms, {result.courses:,} courses, "
        f"{result.sections:,} sections ({result.skipped:,} subject-units already done)."
    )
    if result.failed_requests:
        # Never let a throttled or flaky run look like a clean one.
        print(
            f"\nWARNING: {result.failed_requests:,} requests were abandoned after retries, "
            f"so this run has holes.\nRe-run the same command -- resume skips the units that "
            f"completed, so it will be quick.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="bruinwatch-backfill", description=__doc__)
    parser.add_argument("--from", dest="start", required=True, help="Earliest term code, e.g. 23W")
    parser.add_argument("--to", dest="end", required=True, help="Latest term code, e.g. 26F")
    parser.add_argument(
        "--rate",
        type=float,
        default=5.0,
        help="Requests per second against sa.ucla.edu (default: %(default)s). "
        "This is a politeness budget; there is no reason to raise it.",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--no-resume", action="store_true", help="Redo subject-units already recorded"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan and cost, make no requests"
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
