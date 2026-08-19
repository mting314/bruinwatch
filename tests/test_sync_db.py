"""End-to-end change detection against a real PostgreSQL engine.

The schema uses PostgreSQL-only features (``TEXT[]``, ``INSERT ... ON CONFLICT``
with ``RETURNING``, a partial index), so there is no honest way to fake this on
SQLite. See ``tests/postgres.py`` for where the database comes from -- by
default an in-process PGlite, so no Docker or local Postgres is needed.

The pure decision rules these exercise are also covered, database-free, in
``test_changes.py``.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bruinwatch.db import models as m
from bruinwatch.registrar.types import Course as CourseDTO
from bruinwatch.registrar.types import (
    EnrollmentNumbers,
    EnrollmentStatus,
    Section,
    WaitlistStatus,
)
from bruinwatch.services.sync import _ensure_course, save_section, watched_courses


@pytest_asyncio.fixture
async def seeded(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """A term, subject, course and one user."""
    async with sessions() as session:
        term = m.Term(code="26F", name="Fall 2026", is_active=True)
        subject = m.SubjectArea(code="COM SCI", name="Computer Science")
        session.add_all([term, subject])
        await session.flush()
        course = m.Course(subject_area_id=subject.id, number="32", title="Intro to CS II")
        user = m.User(discord_id=1234567890)
        session.add_all([course, user])
        await session.flush()
        session.add(m.CourseTerm(course_id=course.id, term_id=term.id, section_indices=["%"]))
        await session.commit()
        return {"term_id": term.id, "course_id": course.id, "user_id": user.id}


def make_section(
    status: EnrollmentStatus = EnrollmentStatus.OPEN,
    count: int = 100,
    capacity: int = 200,
    wl_count: int = 0,
) -> Section:
    return Section(
        registrar_id="187096200",
        term_code="26F",
        subject_area_code="COM SCI",
        course_number="32",
        section_label="Lec 1",
        index=1,
        format="Lec",
        enrollment=EnrollmentNumbers(
            enrollment_status=status,
            enrollment_count=count,
            enrollment_capacity=capacity,
            waitlist_status=WaitlistStatus.OPEN,
            waitlist_count=wl_count,
            waitlist_capacity=30,
        ),
    )


async def _count(sessions, model) -> int:
    async with sessions() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def _save(sessions, seeded, section: Section):
    async with sessions() as session:
        result = await save_section(
            session, section, term_id=seeded["term_id"], course_id=seeded["course_id"]
        )
        await session.commit()
        return result


async def _subscribe(sessions, seeded, spots: int | None = None) -> None:
    async with sessions() as session:
        section_id = (
            await session.execute(select(m.Section.id).where(m.Section.registrar_id == "187096200"))
        ).scalar_one()
        session.add(
            m.Subscription(
                user_id=seeded["user_id"], section_id=section_id, notify_below_spots=spots
            )
        )
        await session.commit()


# --------------------------------------------------------------------------


async def test_first_sighting_seeds_history_and_notifies_nobody(sessions, seeded):
    result = await _save(sessions, seeded, make_section())
    assert result.sections_seen == 1
    assert result.history_rows == 1
    assert result.notifications == 0
    assert await _count(sessions, m.NotificationOutbox) == 0


async def test_unchanged_poll_writes_nothing(sessions, seeded):
    await _save(sessions, seeded, make_section())
    result = await _save(sessions, seeded, make_section())
    assert result.history_rows == 0
    assert result.notifications == 0
    # Only the seed row from the first sighting.
    assert await _count(sessions, m.EnrollmentDatum) == 1


async def test_count_change_records_history_without_notifying(sessions, seeded):
    await _save(sessions, seeded, make_section(count=100))
    await _subscribe(sessions, seeded)

    result = await _save(sessions, seeded, make_section(count=101))
    assert result.history_rows == 1
    assert result.notifications == 0
    assert await _count(sessions, m.EnrollmentDatum) == 2
    assert await _count(sessions, m.NotificationOutbox) == 0


async def test_status_change_notifies_every_subscriber(sessions, seeded):
    await _save(sessions, seeded, make_section(status=EnrollmentStatus.FULL, count=200))
    await _subscribe(sessions, seeded)

    result = await _save(sessions, seeded, make_section(status=EnrollmentStatus.OPEN, count=199))
    assert result.notifications == 1

    async with sessions() as session:
        row = (await session.execute(select(m.NotificationOutbox))).scalar_one()
        assert row.previous_status == "Full"
        assert row.new_status == "Open"
        assert row.reason == "status_change"
        assert row.sent_at is None


async def test_upsert_updates_in_place(sessions, seeded):
    """Repeated scrapes must not duplicate the section row."""
    await _save(sessions, seeded, make_section(count=100))
    await _save(sessions, seeded, make_section(count=150))
    assert await _count(sessions, m.Section) == 1

    async with sessions() as session:
        section = (await session.execute(select(m.Section))).scalar_one()
        assert section.enrollment_count == 150


async def test_spot_threshold_fires_once_on_the_crossing(sessions, seeded):
    await _save(sessions, seeded, make_section(count=190, capacity=200))  # 10 left
    await _subscribe(sessions, seeded, spots=5)

    await _save(sessions, seeded, make_section(count=197, capacity=200))  # 3 left -> fires
    assert await _count(sessions, m.NotificationOutbox) == 1

    await _save(sessions, seeded, make_section(count=198, capacity=200))  # 2 left -> quiet
    assert await _count(sessions, m.NotificationOutbox) == 1

    async with sessions() as session:
        row = (await session.execute(select(m.NotificationOutbox))).scalars().first()
        assert row is not None
        assert row.reason == "spots_threshold"


async def test_subscriber_without_threshold_is_not_notified_by_count(sessions, seeded):
    await _save(sessions, seeded, make_section(count=190, capacity=200))
    await _subscribe(sessions, seeded, spots=None)
    await _save(sessions, seeded, make_section(count=199, capacity=200))
    assert await _count(sessions, m.NotificationOutbox) == 0


async def test_watched_courses_deduplicates_across_subscribers(sessions, seeded):
    """The whole point of the subscriptions table: N watchers, one request."""
    await _save(sessions, seeded, make_section())

    async with sessions() as session:
        section_id = (await session.execute(select(m.Section.id))).scalar_one()
        for discord_id in range(2000, 2050):
            user = m.User(discord_id=discord_id)
            session.add(user)
            await session.flush()
            session.add(m.Subscription(user_id=user.id, section_id=section_id))
        await session.commit()

    async with sessions() as session:
        targets = await watched_courses(session)

    # 51 subscribers, one course to fetch.
    assert len(targets) == 1
    term_code, subject_code, number, registrar_ids = targets[0]
    assert (term_code, subject_code, number) == ("26F", "COM SCI", "32")
    assert registrar_ids == {"187096200"}


async def test_watched_courses_ignores_inactive_terms(sessions, seeded):
    await _save(sessions, seeded, make_section())
    await _subscribe(sessions, seeded)

    async with sessions() as session:
        term = (await session.execute(select(m.Term))).scalar_one()
        term.is_active = False
        await session.commit()

    async with sessions() as session:
        assert await watched_courses(session) == []


async def test_search_cold_start_creates_the_course(sessions):
    """A course nobody has catalogued yet must still be watchable."""
    async with sessions() as session:
        session.add_all(
            [
                m.Term(code="26F", name="Fall 2026", is_active=True),
                m.SubjectArea(code="COM SCI", name="Computer Science"),
            ]
        )
        await session.commit()
        term_id = (await session.execute(select(m.Term.id))).scalar_one()

    async with sessions() as session:
        course_id = await _ensure_course(
            session, CourseDTO(subject_area_code="COM SCI", number="32", title="Intro"), term_id
        )
        await session.commit()

    assert course_id is not None
    assert await _count(sessions, m.Course) == 1
    assert await _count(sessions, m.CourseTerm) == 1

    # Idempotent: a second call reuses the row rather than raising.
    async with sessions() as session:
        again = await _ensure_course(
            session, CourseDTO(subject_area_code="COM SCI", number="32"), term_id
        )
        await session.commit()
    assert again == course_id
    assert await _count(sessions, m.Course) == 1


async def test_unknown_subject_area_is_refused(sessions):
    async with sessions() as session:
        session.add(m.Term(code="26F", name="Fall 2026", is_active=True))
        await session.commit()
        term_id = (await session.execute(select(m.Term.id))).scalar_one()

    async with sessions() as session:
        assert (
            await _ensure_course(session, CourseDTO(subject_area_code="NOPE", number="1"), term_id)
            is None
        )


async def test_default_term_is_the_registrar_current_term(sessions):
    """Regression: `/search` with no term used to resolve to Winter 2027.

    Ordering terms by `code` descending puts 27W ahead of the real current term
    26F, because F sorts before S sorts before W.
    """
    from bruinwatch.db import repo

    async with sessions() as session:
        # Stored in the registrar's own dropdown order.
        for position, (code, current) in enumerate(
            [("27S", False), ("27W", False), ("26F", True), ("26S", False)]
        ):
            session.add(
                m.Term(code=code, position=position, is_current=current, is_active=position < 3)
            )
        await session.commit()

    async with sessions() as session:
        assert await repo.default_term_code(session) == "26F"
        assert [t.code for t in await repo.active_terms(session)] == ["27S", "27W", "26F"]


async def test_default_term_falls_back_when_no_current_marker(sessions):
    from bruinwatch.db import repo

    async with sessions() as session:
        for position, code in enumerate(["27S", "27W", "26F"]):
            session.add(m.Term(code=code, position=position, is_current=False, is_active=True))
        await session.commit()

    async with sessions() as session:
        assert await repo.default_term_code(session) == "27S"
