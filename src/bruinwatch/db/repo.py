"""Query helpers shared by the cogs.

Keeping these out of the cogs means the Discord layer never writes SQL and the
queries can be tested without a bot.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from sqlalchemy import Row, Select, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from . import models as m


@dataclasses.dataclass(frozen=True, slots=True)
class SectionView:
    """A denormalized section, ready to render."""

    section_id: int
    registrar_id: str
    term_code: str
    subject_area_code: str
    course_number: str
    course_title: str
    section_label: str
    format: str
    index: int
    days: tuple[str, ...]
    times: tuple[str, ...]
    locations: tuple[str, ...]
    instructors: tuple[str, ...]
    units: str
    enrollment_status: str
    enrollment_count: int
    enrollment_capacity: int
    waitlist_status: str
    waitlist_count: int
    waitlist_capacity: int
    website: str | None = None
    watched: bool = False

    @property
    def title(self) -> str:
        return f"{self.subject_area_code} {self.course_number} {self.section_label}"

    @property
    def spots_left(self) -> int:
        return max(0, self.enrollment_capacity - self.enrollment_count)

    @property
    def url(self) -> str:
        from ..registrar.model import public_results_url

        return public_results_url(self.term_code, self.registrar_id)


_SECTION_COLUMNS = (
    m.Section.id,
    m.Section.registrar_id,
    m.Term.code,
    m.SubjectArea.code,
    m.Course.number,
    m.Course.title,
    m.Section.section_label,
    m.Section.format,
    m.Section.index,
    m.Section.days,
    m.Section.times,
    m.Section.locations,
    m.Section.instructors,
    m.Section.units,
    m.Section.enrollment_status,
    m.Section.enrollment_count,
    m.Section.enrollment_capacity,
    m.Section.waitlist_status,
    m.Section.waitlist_count,
    m.Section.waitlist_capacity,
    m.Section.website,
)


def _section_query() -> Select[Any]:
    return (
        select(*_SECTION_COLUMNS)
        .select_from(m.Section)
        .join(m.Term, m.Term.id == m.Section.term_id)
        .join(m.Course, m.Course.id == m.Section.course_id)
        .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
    )


def _to_view(row: Row[Any], watched: bool = False) -> SectionView:
    return SectionView(
        section_id=row[0],
        registrar_id=row[1],
        term_code=row[2],
        subject_area_code=row[3],
        course_number=row[4],
        course_title=row[5],
        section_label=row[6],
        format=row[7],
        index=row[8],
        days=tuple(row[9] or ()),
        times=tuple(row[10] or ()),
        locations=tuple(row[11] or ()),
        instructors=tuple(row[12] or ()),
        units=row[13] or "",
        enrollment_status=row[14],
        enrollment_count=row[15],
        enrollment_capacity=row[16],
        waitlist_status=row[17],
        waitlist_count=row[18],
        waitlist_capacity=row[19],
        website=row[20],
        watched=watched,
    )


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


async def get_or_create_user(session: AsyncSession, discord_id: int) -> m.User:
    user = (
        await session.execute(select(m.User).where(m.User.discord_id == discord_id))
    ).scalar_one_or_none()
    if user is not None:
        return user
    await session.execute(
        pg_insert(m.User)
        .values(discord_id=discord_id)
        .on_conflict_do_nothing(index_elements=["discord_id"])
    )
    await session.flush()
    return (
        await session.execute(select(m.User).where(m.User.discord_id == discord_id))
    ).scalar_one()


# --------------------------------------------------------------------------
# Terms and catalog
# --------------------------------------------------------------------------


async def active_terms(session: AsyncSession) -> list[m.Term]:
    """Active terms, newest first.

    Ordered by ``position``, never by ``code``: term codes do not sort
    chronologically. Alphabetically ``26F < 26S < 26W``, but the year runs
    Winter, Spring, Fall -- exactly backwards.
    """
    return list(
        (
            await session.execute(
                select(m.Term).where(m.Term.is_active.is_(True)).order_by(m.Term.position)
            )
        )
        .scalars()
        .all()
    )


async def default_term_code(session: AsyncSession) -> str | None:
    """The term to use when a command omits one.

    The registrar's own selected term -- the one people are enrolling in --
    falling back to the newest active term if we have not seen that marker yet.
    """
    current = (
        (
            await session.execute(
                select(m.Term.code).where(m.Term.is_current.is_(True)).order_by(m.Term.position)
            )
        )
        .scalars()
        .first()
    )
    if current is not None:
        return current
    return (
        (
            await session.execute(
                select(m.Term.code).where(m.Term.is_active.is_(True)).order_by(m.Term.position)
            )
        )
        .scalars()
        .first()
    )


async def search_subject_areas(
    session: AsyncSession, query: str, limit: int = 25
) -> list[m.SubjectArea]:
    pattern = f"%{query.strip()}%"
    return list(
        (
            await session.execute(
                select(m.SubjectArea)
                .where(m.SubjectArea.code.ilike(pattern) | m.SubjectArea.name.ilike(pattern))
                .order_by(m.SubjectArea.code)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def search_courses(
    session: AsyncSession,
    subject_area_code: str,
    query: str,
    term_code: str | None = None,
    limit: int = 25,
) -> list[m.Course]:
    stmt = (
        select(m.Course)
        .join(m.SubjectArea)
        .where(m.SubjectArea.code == subject_area_code)
        .order_by(m.Course.number)
        .limit(limit)
    )
    if query.strip():
        pattern = f"%{query.strip()}%"
        stmt = stmt.where(m.Course.number.ilike(pattern) | m.Course.title.ilike(pattern))
    if term_code:
        stmt = stmt.join(m.CourseTerm, m.CourseTerm.course_id == m.Course.id).join(
            m.Term, (m.Term.id == m.CourseTerm.term_id) & (m.Term.code == term_code)
        )
    return list((await session.execute(stmt)).scalars().all())


async def courses_in_subject(
    session: AsyncSession, subject_area_code: str, term_code: str
) -> list[m.Course]:
    return list(
        (
            await session.execute(
                select(m.Course)
                .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
                .join(m.CourseTerm, m.CourseTerm.course_id == m.Course.id)
                .join(m.Term, m.Term.id == m.CourseTerm.term_id)
                .where(m.SubjectArea.code == subject_area_code, m.Term.code == term_code)
                .order_by(m.Course.number)
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


async def sections_for_course(
    session: AsyncSession,
    subject_area_code: str,
    course_number: str,
    term_code: str,
    watcher_id: int | None = None,
) -> list[SectionView]:
    rows = await session.execute(
        _section_query()
        .where(
            m.SubjectArea.code == subject_area_code,
            m.Course.number == course_number,
            m.Term.code == term_code,
        )
        .order_by(m.Section.index, m.Section.section_label)
    )
    views = [_to_view(row) for row in rows]
    if watcher_id is not None and views:
        watched = await watched_section_ids(session, watcher_id)
        views = [dataclasses.replace(v, watched=v.section_id in watched) for v in views]
    return views


async def section_by_id(session: AsyncSession, section_id: int) -> SectionView | None:
    row = (await session.execute(_section_query().where(m.Section.id == section_id))).one_or_none()
    return _to_view(row) if row is not None else None


async def enrollment_history(
    session: AsyncSession, section_id: int, limit: int = 500
) -> list[Row[Any]]:
    rows = await session.execute(
        select(
            m.EnrollmentDatum.created_at,
            m.EnrollmentDatum.enrollment_count,
            m.EnrollmentDatum.enrollment_capacity,
            m.EnrollmentDatum.enrollment_status,
            m.EnrollmentDatum.waitlist_count,
        )
        .where(m.EnrollmentDatum.section_id == section_id)
        .order_by(m.EnrollmentDatum.created_at)
        .limit(limit)
    )
    return list(rows.all())


# --------------------------------------------------------------------------
# Subscriptions
# --------------------------------------------------------------------------


async def watched_section_ids(session: AsyncSession, user_id: int) -> set[int]:
    return set(
        (
            await session.execute(
                select(m.Subscription.section_id).where(m.Subscription.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )


async def watchlist(session: AsyncSession, user_id: int) -> list[SectionView]:
    rows = await session.execute(
        _section_query()
        .join(m.Subscription, m.Subscription.section_id == m.Section.id)
        .where(m.Subscription.user_id == user_id)
        .order_by(m.Term.position, m.SubjectArea.code, m.Course.number, m.Section.index)
    )
    return [_to_view(row, watched=True) for row in rows]


async def subscribe(session: AsyncSession, user_id: int, section_id: int) -> bool:
    """Returns True if a new subscription was created."""
    result = await session.execute(
        pg_insert(m.Subscription)
        .values(user_id=user_id, section_id=section_id)
        .on_conflict_do_nothing(index_elements=["user_id", "section_id"])
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def unsubscribe(session: AsyncSession, user_id: int, section_id: int) -> bool:
    result = await session.execute(
        delete(m.Subscription).where(
            m.Subscription.user_id == user_id, m.Subscription.section_id == section_id
        )
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def clear_subscriptions(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(delete(m.Subscription).where(m.Subscription.user_id == user_id))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def set_spot_threshold(
    session: AsyncSession, user_id: int, section_id: int, spots: int | None
) -> bool:
    row = await session.get(m.Subscription, (user_id, section_id))
    if row is None:
        return False
    row.notify_below_spots = spots
    return True


# --------------------------------------------------------------------------
# Aliases
# --------------------------------------------------------------------------


async def resolve_alias(session: AsyncSession, user_id: int, alias: str) -> str | None:
    return (
        await session.execute(
            select(m.Alias.target).where(m.Alias.user_id == user_id, m.Alias.alias == alias.upper())
        )
    ).scalar_one_or_none()


async def set_alias(session: AsyncSession, user_id: int, alias: str, target: str) -> None:
    await session.execute(
        pg_insert(m.Alias)
        .values(user_id=user_id, alias=alias.upper(), target=target.upper())
        .on_conflict_do_update(index_elements=["user_id", "alias"], set_={"target": target.upper()})
    )


async def remove_alias(session: AsyncSession, user_id: int, alias: str) -> bool:
    result = await session.execute(
        delete(m.Alias).where(m.Alias.user_id == user_id, m.Alias.alias == alias.upper())
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def list_aliases(session: AsyncSession, user_id: int) -> list[tuple[str, str]]:
    rows = await session.execute(
        select(m.Alias.alias, m.Alias.target)
        .where(m.Alias.user_id == user_id)
        .order_by(m.Alias.alias)
    )
    return [(row[0], row[1]) for row in rows]


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------


async def stats(session: AsyncSession) -> dict[str, int]:
    async def count(model: type[Any]) -> int:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())

    return {
        "terms": await count(m.Term),
        "subject_areas": await count(m.SubjectArea),
        "courses": await count(m.Course),
        "sections": await count(m.Section),
        "enrollment_data": await count(m.EnrollmentDatum),
        "users": await count(m.User),
        "subscriptions": await count(m.Subscription),
        "pending_notifications": int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(m.NotificationOutbox)
                    .where(m.NotificationOutbox.sent_at.is_(None))
                )
            ).scalar_one()
        ),
    }
