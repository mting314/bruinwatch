"""Guard against the Alembic migration drifting from the SQLAlchemy models.

Autogenerate needs a live database, so this compares the DDL each side would
emit for PostgreSQL instead. It runs entirely offline, which means CI catches
"I added a column and forgot the migration" on every push.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest
from sqlalchemy import create_mock_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from bruinwatch.db.models import Base

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Cosmetic differences that carry no meaning for the database.
_NOISE = re.compile(r"\s+")
_COMMENT = re.compile(r"--[^\n]*")
#: Alembic's own bookkeeping table has no model, by design.
_IGNORED_TABLES = {"alembic_version"}


def _normalize(statements: list[str]) -> dict[str, str]:
    """Key DDL by the object it creates, whitespace-flattened."""
    out: dict[str, str] = {}
    for raw in statements:
        sql = _NOISE.sub(" ", _COMMENT.sub("", raw)).strip().rstrip(";").strip()
        if not sql:
            continue
        table = re.match(r"CREATE TABLE (\w+)", sql)
        index = re.match(r"CREATE (?:UNIQUE )?INDEX (\w+)", sql)
        if table and table.group(1) not in _IGNORED_TABLES:
            out[f"table:{table.group(1)}"] = sql
        elif index:
            out[f"index:{index.group(1)}"] = sql
    return out


def _models_ddl() -> dict[str, str]:
    statements: list[str] = []

    def collect(sql, *_args, **_kwargs):
        statements.append(str(sql.compile(dialect=engine.dialect)))

    engine = create_mock_engine("postgresql://", collect)
    for table in Base.metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=engine.dialect)))
        for index in table.indexes:
            statements.append(str(CreateIndex(index).compile(dialect=engine.dialect)))
    return _normalize(statements)


def _migration_ddl() -> dict[str, str]:
    """Run the real ``alembic upgrade head --sql`` and read back its DDL.

    Out of process on purpose: it exercises the same command path a deploy uses,
    and Alembic's offline mode writes straight to stdout.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic offline upgrade failed:\n{result.stderr}")
    return _normalize(result.stdout.split(";"))


@pytest.fixture(scope="module")
def ddl() -> tuple[dict[str, str], dict[str, str]]:
    return _models_ddl(), _migration_ddl()


def test_migration_creates_every_model_table(ddl):
    models, migration = ddl
    model_tables = {k for k in models if k.startswith("table:")}
    migration_tables = {k for k in migration if k.startswith("table:")}
    assert model_tables == migration_tables


def test_migration_creates_every_model_index(ddl):
    models, migration = ddl
    model_indexes = {k for k in models if k.startswith("index:")}
    migration_indexes = {k for k in migration if k.startswith("index:")}
    missing = model_indexes - migration_indexes
    assert not missing, f"declared on the model but never created: {sorted(missing)}"


def test_column_sets_match(ddl):
    """Catches the common failure: a new column with no migration."""
    models, migration = ddl

    def columns(sql: str) -> set[str]:
        body = sql[sql.index("(") + 1 : sql.rindex(")")]
        names = set()
        depth = 0
        current = ""
        for char in body:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and depth == 0:
                names.add(current.strip().split(" ")[0])
                current = ""
            else:
                current += char
        names.add(current.strip().split(" ")[0])
        # Table-level clauses share the comma list with real columns.
        keywords = {"PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"}
        return {n for n in names if n and n.isidentifier() and n not in keywords}

    for key, model_sql in models.items():
        if not key.startswith("table:"):
            continue
        assert columns(model_sql) == columns(migration[key]), f"column drift in {key}"


def test_partial_outbox_index_is_partial(ddl):
    """The unsent-notification index must stay partial or it grows forever."""
    _, migration = ddl
    assert "WHERE sent_at IS NULL" in migration["index:ix_outbox_unsent"]
