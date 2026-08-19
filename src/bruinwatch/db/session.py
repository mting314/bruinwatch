"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


def create_engine(
    database_url: str,
    *,
    echo: bool = False,
    pool_size: int = 10,
    max_overflow: int = 5,
    single_connection: bool = False,
) -> AsyncEngine:
    """Build the async engine.

    Pool size is a parameter because the right value differs sharply by
    caller: the bot holds one long-lived pool, a one-shot scrape job needs a
    couple of connections, and a small managed Postgres has few to give.

    ``single_connection`` pins exactly one connection for the engine's whole
    life (``StaticPool``) instead of pooling. That is what PGlite requires --
    it serves one connection at a time and does not complete a graceful close
    -- so it is how the CLIs get integration-tested without a real server. It
    is also harmless for a short one-shot job.
    """
    if single_connection:
        return create_async_engine(database_url, echo=echo, poolclass=StaticPool)
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def wait_for_database(engine: AsyncEngine, attempts: int = 20, delay: float = 0.5) -> None:
    """Block until the database answers, or give up with the last error.

    A job can easily start before its database is reachable: a CI service
    container still booting, a Cloud SQL instance waking, an in-process PGlite
    that accepts on the socket a moment before it can answer a startup packet.
    Failing immediately in those cases is just a flaky job.
    """
    import asyncio

    from sqlalchemy import text

    last: Exception | None = None
    for _ in range(attempts):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            last = exc
            await engine.dispose(close=False)
            await asyncio.sleep(delay)
    raise RuntimeError(f"database never became ready after {attempts} attempts: {last!r}")


@asynccontextmanager
async def transaction(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Open a session, commit on success, roll back on any exception."""
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
