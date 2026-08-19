"""Getting a real PostgreSQL to test against, without Docker.

The schema leans on PostgreSQL-only features -- ``TEXT[]`` columns,
``INSERT ... ON CONFLICT ... RETURNING``, a partial index -- so the change
detection tests need a genuine engine, not SQLite pretending.

Two sources, in order:

1. ``BRUINWATCH_TEST_DATABASE_URL`` if set. CI points this at a Postgres service
   container; locally you can point it at any throwaway database.
2. **PGlite** -- PostgreSQL compiled to WebAssembly, run in-process over a TCP
   socket. Needs Node on PATH and nothing else: no daemon, no Docker, no
   ``initdb``. This is the same trick the sibling ll-predictions repo uses (see
   its ``scripts/dev-up.ts``), which is why the DB tests need no setup here.

If neither is available the DB-backed tests skip; the pure decision rules they
exercise are also covered, database-free, in ``test_changes.py``.

PGlite quirks, all handled by the fixtures in ``conftest.py``:

* it serves **one connection at a time**, so the engine pins a single one
  (``StaticPool``) for the whole session rather than pooling;
* it accepts on the socket slightly before it can answer a startup packet,
  which asyncpg reports as ``AttributeError: server_version`` -- hence the
  connect retry;
* it does not complete a graceful connection shutdown, so the engine is
  disposed with ``close=False``;
* it refuses asyncpg's SSL upgrade and its prepared-statement caching, both
  disabled via URL query parameters above.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import socket
from collections.abc import Iterator
from contextlib import closing, contextmanager

ENV_VAR = "BRUINWATCH_TEST_DATABASE_URL"

#: asyncpg needs both of these to talk to PGlite at all. Expressed as URL
#: query parameters rather than connect_args so that any code taking only a
#: connection string -- the bruinwatch-web and bruinwatch-scrape CLIs, which
#: read one from the environment -- can be pointed at PGlite unchanged.
PGLITE_URL_ARGS = "ssl=disable&prepared_statement_cache_size=0"


@dataclasses.dataclass(frozen=True, slots=True)
class TestDatabase:
    url: str
    connect_args: dict[str, object]
    #: True when this is an in-process PGlite rather than a configured server.
    embedded: bool

    @property
    def connect_attempts(self) -> int:
        # A configured server that is down should fail immediately and say so;
        # only PGlite's racy startup is worth waiting out.
        return 20 if self.embedded else 1


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def pglite_available() -> bool:
    if shutil.which("node") is None:
        return False
    try:
        import py_pglite  # noqa: F401
    except ImportError:
        return False
    return True


@contextmanager
def provide() -> Iterator[TestDatabase | None]:
    """Yield a database to test against, or ``None`` if there isn't one."""
    configured = os.environ.get(ENV_VAR)
    if configured:
        yield TestDatabase(url=configured, connect_args={}, embedded=False)
        return

    if not pglite_available():
        yield None
        return

    from py_pglite import PGliteConfig, PGliteManager

    port = _free_port()
    with PGliteManager(PGliteConfig(use_tcp=True, tcp_port=port, log_level="WARNING")):
        yield TestDatabase(
            url=(
                f"postgresql+asyncpg://postgres:postgres@127.0.0.1:{port}"
                f"/postgres?{PGLITE_URL_ARGS}"
            ),
            connect_args={},
            embedded=True,
        )


def skip_reason() -> str:
    return f"needs PostgreSQL: set {ENV_VAR}, or install Node so the bundled PGlite can provide one"
