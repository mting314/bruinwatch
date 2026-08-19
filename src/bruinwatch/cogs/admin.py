"""Owner-only operations: /admin sync|stats|scraper."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Literal

import discord
import structlog
from discord import app_commands
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import models as m
from ..db import repo
from ..services.scheduler import BURST_INTERVAL, CAMPUS_TZ
from ..ui.embeds import error_embed, info_embed
from .base import BruinWatchCog

if TYPE_CHECKING:
    from ..bot import BruinWatchBot

log = structlog.get_logger(__name__)

JobName = Literal["terms", "subject-areas", "catalog", "all-sections", "watched"]


def _parse_campus_time(value: str) -> dt.datetime:
    """Read ``YYYY-MM-DD HH:MM`` as campus local time."""
    return dt.datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=CAMPUS_TZ)


class Admin(BruinWatchCog):
    group = app_commands.Group(
        name="admin",
        description="Bot owner controls.",
        # Hide from anyone without Manage Server; the owner check below is the
        # real gate, this just declutters everyone else's command list.
        default_permissions=discord.Permissions(manage_guild=True),
    )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        owner_id = self.bot.settings.owner_id
        if owner_id is not None and interaction.user.id == owner_id:
            return True
        await interaction.response.send_message(
            embed=error_embed("That command is owner-only."), ephemeral=True
        )
        return False

    @group.command(name="stats", description="Row counts and scraper state.")
    async def stats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        async with self.sessions() as session:
            counts = await repo.stats(session)

        scraper = self.bot.scraper
        embed = discord.Embed(title="BruinWatch status", colour=discord.Colour.blurple())
        for key, value in counts.items():
            embed.add_field(name=key.replace("_", " ").title(), value=f"{value:,}", inline=True)
        embed.add_field(
            name="Watched poll interval",
            value=str(scraper.poll_interval or "not started"),
            inline=True,
        )
        embed.add_field(
            name="Consecutive failures",
            value=str(scraper.consecutive_failures),
            inline=True,
        )
        embed.add_field(
            name="Circuit breaker",
            value="TRIPPED" if scraper.circuit_open else "closed",
            inline=True,
        )
        embed.add_field(
            name="Scheduled jobs",
            value="\n".join(
                f"`{job.id}` → {job.next_run_time:%Y-%m-%d %H:%M %Z}"
                if job.next_run_time
                else f"`{job.id}` → paused"
                for job in scraper.scheduler.get_jobs()
            )
            or "scheduler not running",
            inline=False,
        )
        await interaction.followup.send(embed=embed)

    @group.command(name="sync", description="Run a scraper job right now.")
    @app_commands.describe(job="Which job to run")
    async def sync(self, interaction: discord.Interaction, job: JobName) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            await self.bot.scraper.run_now(job)
        except Exception as exc:
            log.exception("manual_sync_failed", job=job)
            await interaction.followup.send(embed=error_embed(f"`{job}` failed: `{exc!r}`"))
            return
        await interaction.followup.send(embed=info_embed("Done", f"`{job}` finished."))

    @group.command(name="scraper", description="Pause or resume background scraping.")
    @app_commands.describe(action="pause or resume")
    async def scraper(
        self, interaction: discord.Interaction, action: Literal["pause", "resume"]
    ) -> None:
        scheduler = self.bot.scraper.scheduler
        if not scheduler.running:
            await interaction.response.send_message(
                embed=error_embed("The scheduler isn't running."), ephemeral=True
            )
            return
        if action == "pause":
            scheduler.pause()
            await self.bot.change_presence(
                status=discord.Status.idle,
                activity=discord.Activity(
                    type=discord.ActivityType.watching, name="nothing (paused)"
                ),
            )
        else:
            scheduler.resume()
            await self.bot.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(
                    type=discord.ActivityType.watching, name="UCLA enrollment"
                ),
            )
        await interaction.response.send_message(
            embed=info_embed("Scraper", f"Scraping {action}d."), ephemeral=True
        )

    @group.command(
        name="enrollment-window",
        description="Record an enrollment pass window, which speeds up polling while it is open.",
    )
    @app_commands.describe(
        term="Term code, e.g. 26F",
        name="Pass name, e.g. first_pass",
        start="Start, campus time: YYYY-MM-DD HH:MM",
        end="End, campus time: YYYY-MM-DD HH:MM",
    )
    async def enrollment_window(
        self,
        interaction: discord.Interaction,
        term: str,
        name: str,
        start: str,
        end: str,
    ) -> None:
        """Seed ``enrollment_appointments``.

        The registrar publishes pass dates on a page whose URL changes every
        year, so -- as hotseat.io also does -- these are entered by hand rather
        than scraped. Until at least one window exists the poller never uses its
        fast tier, because there is no moment it knows to be worth bursting for.
        """
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            start_at = _parse_campus_time(start)
            end_at = _parse_campus_time(end)
        except ValueError as exc:
            await interaction.followup.send(embed=error_embed(f"Bad timestamp: {exc}"))
            return
        if end_at <= start_at:
            await interaction.followup.send(embed=error_embed("The window ends before it starts."))
            return

        async with self.transaction() as session:
            term_id = (
                await session.execute(select(m.Term.id).where(m.Term.code == term.upper()))
            ).scalar_one_or_none()
            if term_id is None:
                await interaction.followup.send(
                    embed=error_embed(f"I don't know a term called `{term.upper()}`.")
                )
                return
            await session.execute(
                pg_insert(m.EnrollmentAppointment)
                .values(term_id=term_id, pass_name=name, start_at=start_at, end_at=end_at)
                .on_conflict_do_update(constraint="uq_appointment", set_={"end_at": end_at})
            )

        await interaction.followup.send(
            embed=info_embed(
                "Enrollment window recorded",
                f"**{term.upper()} · {name}**\n"
                f"{start_at:%a %d %b %H:%M} → {end_at:%a %d %b %H:%M} campus time\n\n"
                f"Watched sections will poll every "
                f"{int(BURST_INTERVAL.total_seconds())}s while it is open.",
            )
        )

    @group.command(name="resync-commands", description="Re-register slash commands.")
    async def resync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        synced = await self.bot.tree.sync()
        await interaction.followup.send(
            embed=info_embed("Commands synced", f"{len(synced)} commands registered.")
        )


async def setup(bot: BruinWatchBot) -> None:
    await bot.add_cog(Admin(bot))
