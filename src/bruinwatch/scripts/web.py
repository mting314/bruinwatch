"""Serve the stats site on its own, with no Discord bot.

    uv run bruinwatch-web --port 8080

Read-only over whatever the scraper has collected. Nothing here holds a
long-lived connection to anything, so it scales to zero happily -- unlike the
bot, which must keep a Discord gateway WebSocket open and therefore cannot.

Pair with ``bruinwatch-scrape`` on a schedule and the Discord side can stay
switched off entirely.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal

import structlog
from aiohttp import web

from .. import logging as log_setup
from ..config import Settings
from ..db.session import create_engine, create_session_factory
from ..web.app import build_standalone_app


async def serve(port: int) -> int:
    settings = Settings()
    log_setup.configure(settings.log_level, json_output=settings.log_json)
    log = structlog.get_logger("web")

    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    sessions = create_session_factory(engine)

    app = build_standalone_app(sessions)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("web_listening", port=port, stats="/stats")

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)
    try:
        await stopping.wait()
    finally:
        await runner.cleanup()
        await engine.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="bruinwatch-web", description=__doc__)
    # Cloud Run and most PaaS hand you the port in $PORT.
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    args = parser.parse_args()
    return asyncio.run(serve(args.port))


if __name__ == "__main__":
    raise SystemExit(main())
