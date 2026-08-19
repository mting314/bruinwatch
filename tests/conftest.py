from __future__ import annotations

import asyncio
import os
import pathlib
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import postgres
from bruinwatch.db.models import Base
from bruinwatch.db.session import create_session_factory

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_text():
    def _load(name: str) -> str:
        return (FIXTURES / name).read_text()

    return _load


@pytest.fixture(scope="session")
def database() -> Iterator[postgres.TestDatabase]:
    """A real PostgreSQL for the whole session. See ``tests/postgres.py``.

    Skipping when none is available is right for a contributor without Node,
    but wrong for CI -- a silent skip there would hide the DB tests rotting.
    ``BRUINWATCH_REQUIRE_TEST_DB=1`` turns the skip into a failure.
    """
    with postgres.provide() as db:
        if db is None:
            if os.environ.get("BRUINWATCH_REQUIRE_TEST_DB"):
                pytest.fail(postgres.skip_reason())
            pytest.skip(postgres.skip_reason())
        yield db


@pytest_asyncio.fixture(scope="session")
async def engine(database: postgres.TestDatabase) -> AsyncIterator[AsyncEngine]:
    """One engine, one connection, for the entire session.

    PGlite serves a single connection at a time and neither opens nor closes one
    reliably in a tight loop, so we open exactly one and keep it. ``StaticPool``
    pins it; a real Postgres is equally happy with this.
    """
    engine = create_async_engine(
        database.url, poolclass=StaticPool, connect_args=database.connect_args
    )
    await _wait_until_ready(engine, database.connect_attempts)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # close=False abandons the connection rather than negotiating a shutdown,
    # which PGlite does not reliably complete.
    await engine.dispose(close=False)


async def _wait_until_ready(engine: AsyncEngine, attempts: int) -> None:
    """Open the first connection, retrying while PGlite finishes starting.

    PGlite accepts on the socket slightly before it can answer a startup packet,
    which surfaces as ``AttributeError: server_version`` from asyncpg. A
    configured server gets one attempt, so a typo fails fast.
    """
    last: Exception | None = None
    for _ in range(attempts):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            last = exc
            await engine.dispose(close=False)
            await asyncio.sleep(0.5)
    raise RuntimeError(f"database never became ready: {last!r}")


@pytest_asyncio.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory over an empty schema.

    Tables are truncated rather than dropped and recreated: one statement
    instead of a full DDL cycle, and it keeps the single pinned connection.
    """
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield create_session_factory(engine)
