"""Entrypoint: ``python -m bruinwatch`` or the ``bruinwatch`` console script."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

import structlog

from . import health
from . import logging as log_setup
from .bot import BruinWatchBot
from .config import Settings


async def main() -> int:
    try:
        settings = Settings()  # type: ignore[call-arg]  # pydantic reads the env
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2

    log_setup.configure(settings.log_level, json_output=settings.log_json)
    log = structlog.get_logger("bruinwatch")

    bot = BruinWatchBot(settings)
    runner = None

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stopping.set)

    async def shutdown_on_signal() -> None:
        await stopping.wait()
        log.info("shutdown_signal_received")
        await bot.close()

    watcher = asyncio.create_task(shutdown_on_signal())
    try:
        if settings.healthcheck_port:
            runner = await health.start(bot, settings.healthcheck_port)
        await bot.start(settings.discord_token.get_secret_value())
    except Exception:
        log.exception("fatal")
        return 1
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        if runner is not None:
            await runner.cleanup()
        if not bot.is_closed():
            await bot.close()
    return 0


def run() -> None:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    run()
