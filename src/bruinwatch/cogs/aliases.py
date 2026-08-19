"""Subject-area shorthands: /alias set|remove|list."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..db import repo
from ..ui.embeds import error_embed, info_embed
from .base import BruinWatchCog

if TYPE_CHECKING:
    from ..bot import BruinWatchBot


class Aliases(BruinWatchCog):
    group = app_commands.Group(
        name="alias", description="Shorthand names for subject areas (CS -> COM SCI)."
    )

    @group.command(name="set", description="Map a shorthand to a subject area.")
    @app_commands.describe(
        alias="What you want to type, e.g. CS",
        target="The registrar's subject area, e.g. COM SCI",
    )
    async def set_alias(self, interaction: discord.Interaction, alias: str, target: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        alias_key = alias.strip().upper()
        target_key = target.strip().upper()

        if not alias_key or not target_key:
            await interaction.followup.send(embed=error_embed("Both fields are required."))
            return
        if alias_key == target_key:
            await interaction.followup.send(
                embed=error_embed("That alias is the same as its target.")
            )
            return

        async with self.transaction() as session:
            known = await repo.search_subject_areas(session, target_key, limit=50)
            if not any(area.code == target_key for area in known):
                suggestions = ", ".join(f"`{a.code}`" for a in known[:5])
                await interaction.followup.send(
                    embed=error_embed(
                        f"`{target_key}` isn't a subject area I know about."
                        + (f" Did you mean {suggestions}?" if suggestions else "")
                    )
                )
                return
            user = await repo.get_or_create_user(session, interaction.user.id)
            await repo.set_alias(session, user.id, alias_key, target_key)

        await interaction.followup.send(
            embed=info_embed(
                "Alias set",
                f"`{alias_key}` → `{target_key}`\nTry `/search subject:{alias_key} number:1`.",
            )
        )

    @set_alias.autocomplete("target")
    async def _target_ac(self, interaction: discord.Interaction, current: str):
        return await self.subject_autocomplete(interaction, current)

    @group.command(name="remove", description="Unmap a shorthand.")
    @app_commands.describe(alias="The shorthand to remove, e.g. CS")
    async def remove_alias(self, interaction: discord.Interaction, alias: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        async with self.transaction() as session:
            user = await repo.get_or_create_user(session, interaction.user.id)
            removed = await repo.remove_alias(session, user.id, alias)

        if removed:
            await interaction.followup.send(
                embed=info_embed("Alias removed", f"`{alias.upper()}` is no longer mapped.")
            )
        else:
            await interaction.followup.send(
                embed=error_embed(f"You don't have an alias called `{alias.upper()}`.")
            )

    @remove_alias.autocomplete("alias")
    async def _alias_ac(self, interaction: discord.Interaction, current: str):
        async with self.sessions() as session:
            user = await repo.get_or_create_user(session, interaction.user.id)
            await session.commit()
            pairs = await repo.list_aliases(session, user.id)
        return [
            app_commands.Choice(name=f"{alias} → {target}", value=alias)
            for alias, target in pairs
            if current.upper() in alias
        ][:25]

    @group.command(name="list", description="Show the shorthands you've set.")
    async def list_aliases(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        async with self.sessions() as session:
            user = await repo.get_or_create_user(session, interaction.user.id)
            await session.commit()
            pairs = await repo.list_aliases(session, user.id)

        if not pairs:
            await interaction.followup.send(
                embed=info_embed(
                    "No aliases yet",
                    "Set one with `/alias set alias:CS target:COM SCI`.",
                )
            )
            return

        embed = discord.Embed(title="Your aliases", colour=discord.Colour.blurple())
        for alias, target in pairs:
            embed.add_field(name=alias, value=target, inline=True)
        await interaction.followup.send(embed=embed)


async def setup(bot: BruinWatchBot) -> None:
    await bot.add_cog(Aliases(bot))
