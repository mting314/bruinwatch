"""Shared cog plumbing: session access, autocomplete, alias resolution."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

import discord
import structlog
from discord import app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import repo
from ..db.session import transaction

if TYPE_CHECKING:
    from ..bot import BruinWatchBot

log = structlog.get_logger(__name__)


class BruinWatchCog(commands.Cog):
    def __init__(self, bot: BruinWatchBot) -> None:
        self.bot = bot

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        return self.bot.sessions

    def transaction(self) -> AbstractAsyncContextManager[AsyncSession]:
        return transaction(self.bot.sessions)

    async def resolve_subject(self, discord_id: int, raw: str) -> str:
        """Apply the user's aliases, then normalize casing.

        ``cs`` becomes ``COM SCI`` if that alias is set; otherwise the input is
        just upper-cased, which is what the registrar expects.
        """
        candidate = raw.strip().upper()
        async with self.sessions() as session:
            user = await repo.get_or_create_user(session, discord_id)
            await session.commit()
            target = await repo.resolve_alias(session, user.id, candidate)
        return target or candidate

    async def resolve_term(self, term: str | None) -> str | None:
        if term:
            return term.strip().upper()
        async with self.sessions() as session:
            return await repo.default_term_code(session)

    # -- autocomplete -----------------------------------------------------

    async def subject_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        async with self.sessions() as session:
            user = await repo.get_or_create_user(session, interaction.user.id)
            await session.commit()
            choices: list[app_commands.Choice[str]] = []

            # Surface the user's own aliases first; they typed them for a reason.
            for alias, target in await repo.list_aliases(session, user.id):
                if current.upper() in alias and len(choices) < 5:
                    choices.append(app_commands.Choice(name=f"{alias} → {target}", value=target))

            for area in await repo.search_subject_areas(session, current, limit=25 - len(choices)):
                choices.append(
                    app_commands.Choice(name=f"{area.name} ({area.code})"[:100], value=area.code)
                )
        return choices

    async def course_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        raw_subject = (interaction.namespace.subject or "").strip()
        if not raw_subject:
            return []
        subject = await self.resolve_subject(interaction.user.id, raw_subject)
        term = await self.resolve_term(getattr(interaction.namespace, "term", None))
        async with self.sessions() as session:
            courses = await repo.search_courses(session, subject, current, term)
            return [
                app_commands.Choice(
                    name=f"{course.number} — {course.title}"[:100], value=course.number
                )
                for course in courses
            ]

    async def term_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        async with self.sessions() as session:
            terms = await repo.active_terms(session)
        return [
            app_commands.Choice(name=f"{t.name or t.code} ({t.code})", value=t.code)
            for t in terms
            if current.upper() in t.code.upper() or current.lower() in (t.name or "").lower()
        ][:25]

    # -- first-run greeting -----------------------------------------------

    async def greet_once(self, user: discord.User | discord.Member) -> None:
        """DM a one-time hello.

        The previous implementation decided this by downloading the user's
        entire DM history on every single command; a boolean column is enough.
        """
        async with self.transaction() as session:
            row = await repo.get_or_create_user(session, user.id)
            if row.dm_greeted:
                return
            row.dm_greeted = True

        try:
            await user.send(
                "Thanks for using BruinWatch! You can run every command here in "
                "DMs too, if you'd rather not spam a server."
            )
        except discord.HTTPException:
            # Closed DMs are fine; we just won't be able to notify them later.
            log.info("greeting_dm_blocked", discord_id=user.id)
