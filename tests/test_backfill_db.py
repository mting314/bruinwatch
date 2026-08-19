"""Backfill behaviour that needs a database: term creation and resume."""

from __future__ import annotations

from sqlalchemy import select

from bruinwatch.db import models as m
from bruinwatch.registrar.types import term_position
from bruinwatch.services import backfill


async def test_ensure_term_creates_an_inactive_historical_term(sessions):
    await backfill.ensure_term(sessions, "23F")

    async with sessions() as session:
        term = (await session.execute(select(m.Term))).scalar_one()
    assert term.code == "23F"
    assert term.name == "Fall 2023"
    assert term.position == term_position("23F")
    # Crucial: the poller must never spend requests on a term that has ended.
    assert term.is_active is False
    assert term.is_current is False


async def test_ensure_term_is_idempotent(sessions):
    await backfill.ensure_term(sessions, "23F")
    await backfill.ensure_term(sessions, "23F")
    async with sessions() as session:
        assert len((await session.execute(select(m.Term))).scalars().all()) == 1


async def test_ensure_term_does_not_deactivate_a_live_term(sessions):
    """A backfill overlapping the current term must not switch off polling."""
    async with sessions() as session:
        session.add(
            m.Term(code="26F", name="Fall 2026", position=0, is_current=True, is_active=True)
        )
        await session.commit()

    await backfill.ensure_term(sessions, "26F")

    async with sessions() as session:
        term = (await session.execute(select(m.Term))).scalar_one()
    assert term.is_active is True
    assert term.is_current is True


async def test_completed_units_scopes_to_the_term(sessions):
    async with sessions() as session:
        session.add_all(
            [
                m.BackfillProgress(term_code="23F", subject_area_code="COM SCI"),
                m.BackfillProgress(term_code="23F", subject_area_code="MATH"),
                m.BackfillProgress(term_code="24F", subject_area_code="ECON"),
            ]
        )
        await session.commit()

    async with sessions() as session:
        assert await backfill.completed_units(session, "23F") == {"COM SCI", "MATH"}
        assert await backfill.completed_units(session, "24F") == {"ECON"}
        assert await backfill.completed_units(session, "25F") == set()


async def test_progress_records_a_zero_yield_subject(sessions):
    """A subject with no courses is a completed unit, not a missing one --
    which is why progress is recorded explicitly rather than inferred from
    whether any rows landed."""
    async with sessions() as session:
        session.add(
            m.BackfillProgress(term_code="23F", subject_area_code="EMPTY", courses=0, sections=0)
        )
        await session.commit()

    async with sessions() as session:
        assert "EMPTY" in await backfill.completed_units(session, "23F")
