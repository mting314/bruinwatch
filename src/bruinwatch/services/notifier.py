"""Delivers queued change notifications as Discord DMs.

Runs as its own loop, independent of scraping. Change events are already durably
recorded in ``notification_outbox``, so this can crash, restart, or fall behind
without losing or duplicating a message: a row is only stamped ``sent_at`` after
Discord accepts it.

It also replaces the old ``_get_all_users`` helper, which paginated every member
of every guild the bot was in on every 15-second tick purely to turn IDs into
user objects.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt

import discord
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import models as m

log = structlog.get_logger(__name__)

#: How often to drain the outbox.
POLL_SECONDS = 5.0
#: Rows per drain, to bound how long one pass holds a session.
BATCH_SIZE = 50
#: Give up on a row after this many delivery attempts (blocked DMs, deleted
#: accounts) so it stops being retried forever.
MAX_ATTEMPTS = 5

STATUS_EMOJI = {
    "Open": "\N{LARGE GREEN CIRCLE}",
    "Waitlist": "\N{LARGE YELLOW CIRCLE}",
    "Full": "\N{LARGE RED CIRCLE}",
    "Closed": "\N{LARGE RED CIRCLE}",
    "Cancelled": "\N{CROSS MARK}",
    "Tentative": "\N{WHITE QUESTION MARK ORNAMENT}",
}


class Notifier:
    """Drains ``notification_outbox`` into Discord DMs."""

    def __init__(self, bot: discord.Client, factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.factory = factory
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="notifier")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                sent = await self.drain_once()
                # Only sleep when the queue is empty, so a burst of changes
                # (which is exactly what an enrollment pass produces) drains at
                # full speed instead of 50 messages every 5 seconds.
                if sent < BATCH_SIZE:
                    await asyncio.sleep(POLL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("notifier_drain_failed")
                await asyncio.sleep(POLL_SECONDS)

    async def drain_once(self) -> int:
        """Deliver up to ``BATCH_SIZE`` pending notifications. Returns the count."""
        async with self.factory() as session:
            pending = await self._claim(session)
            if not pending:
                return 0

            for outbox_id, discord_id, payload in pending:
                delivered = await self._deliver(discord_id, payload)
                row = await session.get(m.NotificationOutbox, outbox_id)
                if row is None:
                    continue
                row.attempts += 1
                if delivered or row.attempts >= MAX_ATTEMPTS:
                    # Stamping a permanently-undeliverable row as sent is
                    # deliberate: it retires the row instead of retrying a
                    # blocked DM forever.
                    row.sent_at = dt.datetime.now(dt.UTC)
                    if not delivered:
                        log.warning(
                            "notification_abandoned",
                            outbox_id=outbox_id,
                            attempts=row.attempts,
                        )
            await session.commit()
            return len(pending)

    async def _claim(self, session: AsyncSession) -> list[tuple[int, int, NotificationPayload]]:
        rows = await session.execute(
            select(
                m.NotificationOutbox.id,
                m.User.discord_id,
                m.NotificationOutbox.previous_status,
                m.NotificationOutbox.new_status,
                m.NotificationOutbox.reason,
                m.Section.section_label,
                m.Section.registrar_id,
                m.Section.enrollment_count,
                m.Section.enrollment_capacity,
                m.SubjectArea.code,
                m.Course.number,
                m.Course.title,
                m.Term.code,
            )
            .select_from(m.NotificationOutbox)
            .join(m.User, m.User.id == m.NotificationOutbox.user_id)
            .join(m.Section, m.Section.id == m.NotificationOutbox.section_id)
            .join(m.Course, m.Course.id == m.Section.course_id)
            .join(m.SubjectArea, m.SubjectArea.id == m.Course.subject_area_id)
            .join(m.Term, m.Term.id == m.Section.term_id)
            .where(m.NotificationOutbox.sent_at.is_(None))
            .order_by(m.NotificationOutbox.created_at)
            .limit(BATCH_SIZE)
            # Lets several bot instances drain the same queue without
            # double-sending; harmless with one instance.
            .with_for_update(skip_locked=True, of=m.NotificationOutbox)
        )
        return [
            (
                row[0],
                row[1],
                NotificationPayload(
                    previous_status=row[2],
                    new_status=row[3],
                    reason=row[4],
                    section_label=row[5],
                    registrar_id=row[6],
                    enrollment_count=row[7],
                    enrollment_capacity=row[8],
                    subject_area_code=row[9],
                    course_number=row[10],
                    course_title=row[11],
                    term_code=row[12],
                ),
            )
            for row in rows
        ]

    async def _deliver(self, discord_id: int, payload: NotificationPayload) -> bool:
        user = self.bot.get_user(discord_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(discord_id)
            except (discord.NotFound, discord.HTTPException) as exc:
                log.warning("notify_user_unresolvable", discord_id=discord_id, error=str(exc))
                return False
            except Exception as exc:
                log.warning("notify_user_lookup_error", discord_id=discord_id, error=repr(exc))
                return False
        try:
            await user.send(embed=payload.to_embed())
        except discord.Forbidden:
            log.info("notify_dms_closed", discord_id=discord_id)
            return False
        except discord.HTTPException as exc:
            log.warning("notify_send_failed", discord_id=discord_id, error=str(exc))
            return False
        except Exception as exc:
            # Deliberately broad. Anything escaping here aborts the whole drain
            # before `attempts` is incremented, so one unexpected error would
            # make the batch retry forever.
            log.warning("notify_send_error", discord_id=discord_id, error=repr(exc))
            return False
        return True


class NotificationPayload:
    """Everything needed to render one notification, read in a single query."""

    __slots__ = (
        "course_number",
        "course_title",
        "enrollment_capacity",
        "enrollment_count",
        "new_status",
        "previous_status",
        "reason",
        "registrar_id",
        "section_label",
        "subject_area_code",
        "term_code",
    )

    def __init__(
        self,
        *,
        previous_status: str,
        new_status: str,
        reason: str,
        section_label: str,
        registrar_id: str,
        enrollment_count: int,
        enrollment_capacity: int,
        subject_area_code: str,
        course_number: str,
        course_title: str,
        term_code: str,
    ) -> None:
        self.previous_status = previous_status
        self.new_status = new_status
        self.reason = reason
        self.section_label = section_label
        self.registrar_id = registrar_id
        self.enrollment_count = enrollment_count
        self.enrollment_capacity = enrollment_capacity
        self.subject_area_code = subject_area_code
        self.course_number = course_number
        self.course_title = course_title
        self.term_code = term_code

    def to_embed(self) -> discord.Embed:
        from ..registrar.model import public_results_url

        opened = self.new_status in ("Open", "Waitlist")
        title = f"{self.subject_area_code} {self.course_number} {self.section_label}"

        if self.reason == "spots_threshold":
            headline = f"Only {max(0, self.enrollment_capacity - self.enrollment_count)} spots left"
        else:
            headline = f"{self.previous_status} \N{RIGHTWARDS ARROW} {self.new_status}"

        embed = discord.Embed(
            title=f"{STATUS_EMOJI.get(self.new_status, '')} {title}".strip(),
            description=f"**{headline}**\n{self.course_title}",
            colour=discord.Colour.green() if opened else discord.Colour.red(),
            url=public_results_url(self.term_code, self.registrar_id),
            timestamp=dt.datetime.now(dt.UTC),
        )
        embed.add_field(name="Term", value=self.term_code, inline=True)
        embed.add_field(
            name="Enrollment",
            value=f"{self.enrollment_count}/{self.enrollment_capacity}",
            inline=True,
        )
        embed.set_footer(text="Use /unwatch to stop tracking this section.")
        return embed
