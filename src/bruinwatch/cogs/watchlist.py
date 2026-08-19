"""Watchlist management: /watchlist, /unwatch, /notify."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..db import repo
from ..db.repo import SectionView
from ..ui.embeds import error_embed, info_embed
from ..ui.views import WatchlistView
from .base import BruinWatchCog

if TYPE_CHECKING:
    from ..bot import BruinWatchBot


class Watchlist(BruinWatchCog):
    @app_commands.command(description="See and manage the sections you're watching.")
    async def watchlist(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        sections = await self._load(interaction.user.id)

        if not sections:
            await interaction.followup.send(
                embed=info_embed(
                    "Your watchlist is empty",
                    "Use `/search` to find a class, then pick a section from the dropdown.",
                )
            )
            return

        view = WatchlistView(interaction.user.id, sections, self._remove, self._clear)
        view.message = await interaction.followup.send(embed=view.render(), view=view, wait=True)

    @app_commands.command(description="Stop watching a section.")
    @app_commands.describe(
        subject="Subject area, e.g. COM SCI",
        number="Catalog number, e.g. 32",
    )
    async def unwatch(self, interaction: discord.Interaction, subject: str, number: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        resolved = await self.resolve_subject(interaction.user.id, subject)
        number = number.strip().upper()

        async with self.transaction() as session:
            user = await repo.get_or_create_user(session, interaction.user.id)
            watching = await repo.watchlist(session, user.id)
            matches = [
                s for s in watching if s.subject_area_code == resolved and s.course_number == number
            ]
            for match in matches:
                await repo.unsubscribe(session, user.id, match.section_id)

        if not matches:
            await interaction.followup.send(
                embed=error_embed(f"You aren't watching any sections of {resolved} {number}.")
            )
            return
        await interaction.followup.send(
            embed=info_embed(
                "Removed",
                "\n".join(f"• {s.title} ({s.term_code})" for s in matches),
            )
        )

    @unwatch.autocomplete("subject")
    async def _unwatch_subject_ac(self, interaction: discord.Interaction, current: str):
        return await self.subject_autocomplete(interaction, current)

    @app_commands.command(
        description="Also get pinged when an open section drops below N spots left."
    )
    @app_commands.describe(
        subject="Subject area, e.g. COM SCI",
        number="Catalog number, e.g. 32",
        spots="Alert when this many spots or fewer remain. Use 0 to turn it off.",
    )
    async def notify(
        self,
        interaction: discord.Interaction,
        subject: str,
        number: str,
        spots: app_commands.Range[int, 0, 500],
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        resolved = await self.resolve_subject(interaction.user.id, subject)
        number = number.strip().upper()
        threshold = spots or None

        async with self.transaction() as session:
            user = await repo.get_or_create_user(session, interaction.user.id)
            watching = await repo.watchlist(session, user.id)
            matches = [
                s for s in watching if s.subject_area_code == resolved and s.course_number == number
            ]
            for match in matches:
                await repo.set_spot_threshold(session, user.id, match.section_id, threshold)

        if not matches:
            await interaction.followup.send(
                embed=error_embed(
                    f"You aren't watching {resolved} {number} yet — add it with `/search` first."
                )
            )
            return

        message = (
            f"I'll also ping you when one of these drops to **{threshold} spots or fewer**."
            if threshold is not None
            else "Spots-left alerts turned off; you'll still get status changes."
        )
        await interaction.followup.send(
            embed=info_embed(
                f"{resolved} {number}",
                message + "\n\n" + "\n".join(f"• {s.title}" for s in matches),
            )
        )

    @notify.autocomplete("subject")
    async def _notify_subject_ac(self, interaction: discord.Interaction, current: str):
        return await self.subject_autocomplete(interaction, current)

    # -- internals ---------------------------------------------------------

    async def _load(self, discord_id: int) -> list[SectionView]:
        async with self.sessions() as session:
            user = await repo.get_or_create_user(session, discord_id)
            await session.commit()
            return await repo.watchlist(session, user.id)

    async def _remove(self, interaction: discord.Interaction, chosen: SectionView) -> None:
        async with self.transaction() as session:
            user = await repo.get_or_create_user(session, interaction.user.id)
            await repo.unsubscribe(session, user.id, chosen.section_id)

        remaining = await self._load(interaction.user.id)
        if not remaining:
            await interaction.response.edit_message(
                embed=info_embed("Watchlist cleared", f"Removed {chosen.title}."), view=None
            )
            return

        view = WatchlistView(interaction.user.id, remaining, self._remove, self._clear)
        view.message = interaction.message
        await interaction.response.edit_message(embed=view.render(), view=view)

    async def _clear(self, interaction: discord.Interaction) -> None:
        async with self.transaction() as session:
            user = await repo.get_or_create_user(session, interaction.user.id)
            removed = await repo.clear_subscriptions(session, user.id)

        await interaction.response.edit_message(
            embed=info_embed(
                "Watchlist cleared",
                f"Removed {removed} section{'s' if removed != 1 else ''}. "
                "You won't get any more notifications until you add something.",
            ),
            view=None,
        )


async def setup(bot: BruinWatchBot) -> None:
    await bot.add_cog(Watchlist(bot))
