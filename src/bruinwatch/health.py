"""A tiny HTTP health endpoint.

Replaces the old bare ``socket.bind`` that existed only to satisfy Heroku's port
check and answered nothing. This actually reports whether the bot is connected
and whether the scraper's circuit breaker has tripped, so a platform health
check means something.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from aiohttp import web

if TYPE_CHECKING:
    from .bot import BruinWatchBot

log = structlog.get_logger(__name__)


def build_app(bot: BruinWatchBot) -> web.Application:
    app = web.Application()

    async def healthz(_: web.Request) -> web.Response:
        ready = bot.is_ready() and not bot.is_closed()
        payload = {
            "ready": ready,
            "latency_ms": round(bot.latency * 1000, 1) if ready else None,
            "guilds": len(bot.guilds),
            "scraper": {
                "running": bot.scraper.scheduler.running,
                "poll_interval_s": (
                    bot.scraper.poll_interval.total_seconds() if bot.scraper.poll_interval else None
                ),
                "circuit_open": bot.scraper.circuit_open,
                "consecutive_failures": bot.scraper.consecutive_failures,
            },
        }
        return web.json_response(payload, status=200 if ready else 503)

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", healthz)
    return app


async def start(bot: BruinWatchBot, port: int) -> web.AppRunner:
    runner = web.AppRunner(build_app(bot))
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("healthcheck_listening", port=port)
    return runner
