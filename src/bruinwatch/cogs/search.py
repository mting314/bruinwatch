"""Class lookup: /search, /subject, /history, /about."""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

import discord
import structlog
from discord import app_commands
from sqlalchemy import Row

from ..db import repo
from ..db.repo import SectionView
from ..registrar.types import Course as CourseDTO
from ..services.sync import sync_course_sections
from ..ui.embeds import about_embed, error_embed, info_embed, section_embed
from ..ui.views import Paginator, SectionPicker
from .base import BruinWatchCog

if TYPE_CHECKING:
    from ..bot import BruinWatchBot

log = structlog.get_logger(__name__)

#: Sections per page when listing a whole subject area.
SUBJECT_PAGE_SIZE = 10


class Search(BruinWatchCog):
    @app_commands.command(description="Look up a UCLA class and optionally watch a section.")
    @app_commands.describe(
        subject="Subject area, e.g. COM SCI (your aliases work here too)",
        number="Catalog number, e.g. 32 or M151B",
        term="Term code, e.g. 26F. Defaults to the current term.",
    )
    async def search(
        self,
        interaction: discord.Interaction,
        subject: str,
        number: str,
        term: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        await self.greet_once(interaction.user)

        resolved_subject = await self.resolve_subject(interaction.user.id, subject)
        resolved_term = await self.resolve_term(term)
        if resolved_term is None:
            await interaction.followup.send(
                embed=error_embed("I don't know about any terms yet — try again in a minute."),
            )
            return

        number = number.strip().upper()
        sections = await self._sections(
            interaction.user.id, resolved_subject, number, resolved_term
        )

        if not sections:
            await interaction.followup.send(
                embed=error_embed(
                    f"No sections found for **{resolved_subject} {number}** in "
                    f"**{resolved_term}**.\nSubject areas use the registrar's own "
                    f"abbreviations (`COM SCI`, `C&S BIO`). Try `/subject` to browse."
                )
            )
            return

        view = SectionPicker(interaction.user.id, sections, self._toggle_watch)
        embeds = [section_embed(s) for s in sections[:3]]
        content = (
            f"Showing 3 of {len(sections)} sections; the dropdown lists them all."
            if len(sections) > 3
            else ""
        )

        view.message = await interaction.followup.send(content, embeds=embeds, view=view, wait=True)

    @search.autocomplete("subject")
    async def _search_subject_ac(self, interaction: discord.Interaction, current: str):
        return await self.subject_autocomplete(interaction, current)

    @search.autocomplete("number")
    async def _search_number_ac(self, interaction: discord.Interaction, current: str):
        return await self.course_autocomplete(interaction, current)

    @search.autocomplete("term")
    async def _search_term_ac(self, interaction: discord.Interaction, current: str):
        return await self.term_autocomplete(interaction, current)

    # ----------------------------------------------------------------------

    @app_commands.command(description="Browse every course offered under a subject area.")
    @app_commands.describe(
        subject="Subject area, e.g. COM SCI",
        term="Term code, e.g. 26F. Defaults to the current term.",
    )
    async def subject(
        self, interaction: discord.Interaction, subject: str, term: str | None = None
    ) -> None:
        await interaction.response.defer(thinking=True)
        await self.greet_once(interaction.user)

        resolved_subject = await self.resolve_subject(interaction.user.id, subject)
        resolved_term = await self.resolve_term(term)
        if resolved_term is None:
            await interaction.followup.send(embed=error_embed("No terms loaded yet."))
            return

        async with self.sessions() as session:
            courses = await repo.courses_in_subject(session, resolved_subject, resolved_term)

        if not courses:
            await interaction.followup.send(
                embed=error_embed(
                    f"Nothing found for **{resolved_subject}** in **{resolved_term}**. "
                    f"The catalog syncs daily; a brand new term may not be loaded yet."
                )
            )
            return

        pages = []
        for start in range(0, len(courses), SUBJECT_PAGE_SIZE):
            chunk = courses[start : start + SUBJECT_PAGE_SIZE]
            pages.append(
                discord.Embed(
                    title=f"{resolved_subject} — {resolved_term}",
                    description="\n".join(f"**{c.number}** — {c.title}" for c in chunk),
                    colour=discord.Colour.blue(),
                ).set_footer(
                    text=f"Courses {start + 1}–{start + len(chunk)} of {len(courses)} · "
                    f"/search for section details"
                )
            )

        view = Paginator(interaction.user.id, pages)
        view.message = await interaction.followup.send(embed=pages[0], view=view, wait=True)

    @subject.autocomplete("subject")
    async def _subject_subject_ac(self, interaction: discord.Interaction, current: str):
        return await self.subject_autocomplete(interaction, current)

    @subject.autocomplete("term")
    async def _subject_term_ac(self, interaction: discord.Interaction, current: str):
        return await self.term_autocomplete(interaction, current)

    # ----------------------------------------------------------------------

    @app_commands.command(description="Show a section's enrollment over time.")
    @app_commands.describe(
        subject="Subject area, e.g. COM SCI",
        number="Catalog number, e.g. 32",
        term="Term code, e.g. 26F",
    )
    async def history(
        self,
        interaction: discord.Interaction,
        subject: str,
        number: str,
        term: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        resolved_subject = await self.resolve_subject(interaction.user.id, subject)
        resolved_term = await self.resolve_term(term)
        if resolved_term is None:
            await interaction.followup.send(embed=error_embed("No terms loaded yet."))
            return

        async with self.sessions() as session:
            sections = await repo.sections_for_course(
                session, resolved_subject, number.strip().upper(), resolved_term
            )
            if not sections:
                await interaction.followup.send(
                    embed=error_embed("I have no data for that class yet.")
                )
                return

            pages = []
            for view in sections:
                rows = await repo.enrollment_history(session, view.section_id)
                pages.append(_history_embed(view, rows))

        paginator = Paginator(interaction.user.id, pages)
        paginator.message = await interaction.followup.send(
            embed=pages[0], view=paginator, wait=True
        )

    @history.autocomplete("subject")
    async def _history_subject_ac(self, interaction: discord.Interaction, current: str):
        return await self.subject_autocomplete(interaction, current)

    @history.autocomplete("number")
    async def _history_number_ac(self, interaction: discord.Interaction, current: str):
        return await self.course_autocomplete(interaction, current)

    @app_commands.command(description="What this bot does.")
    async def about(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=about_embed())

    # -- internals ---------------------------------------------------------

    async def _sections(
        self, discord_id: int, subject: str, number: str, term: str
    ) -> list[SectionView]:
        """Read sections from the DB, scraping on demand if we have none.

        The hourly sweep keeps the catalog warm, so this is only a cold-start
        path -- but it means a brand new bot is useful before the first sweep.
        """
        async with self.sessions() as session:
            user = await repo.get_or_create_user(session, discord_id)
            await session.commit()
            sections = await repo.sections_for_course(
                session, subject, number, term, watcher_id=user.id
            )
        if sections:
            return sections

        await sync_course_sections(
            self.bot.registrar,
            self.sessions,
            CourseDTO(subject_area_code=subject, number=number),
            term,
        )
        async with self.sessions() as session:
            user = await repo.get_or_create_user(session, discord_id)
            await session.commit()
            return await repo.sections_for_course(
                session, subject, number, term, watcher_id=user.id
            )

    async def _toggle_watch(
        self,
        interaction: discord.Interaction,
        picker: SectionPicker,
        chosen: SectionView,
    ) -> bool:
        """Subscribe or unsubscribe, and report the resulting state."""
        now_watching = not chosen.watched
        async with self.transaction() as session:
            user = await repo.get_or_create_user(session, interaction.user.id)
            if now_watching:
                await repo.subscribe(session, user.id, chosen.section_id)
            else:
                await repo.unsubscribe(session, user.id, chosen.section_id)

        embed = info_embed(
            f"{'Now watching' if now_watching else 'Stopped watching'}: {chosen.title}",
            f"{chosen.course_title} · {chosen.term_code}\n"
            + (
                "I'll DM you when its enrollment status changes."
                if now_watching
                else "You'll no longer get notifications for this section."
            ),
        )
        embed.colour = discord.Colour.green() if now_watching else discord.Colour.dark_grey()
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return now_watching


def _history_embed(view: SectionView, rows: list[Row[Any]]) -> discord.Embed:
    """Render an enrollment series as a sparkline plus recent transitions."""
    if not rows:
        return discord.Embed(
            title=f"{view.title} — no history yet",
            description="I only have data from the moment I first saw this section.",
            colour=discord.Colour.light_grey(),
        )

    counts = [row[1] for row in rows]
    capacity = max((row[2] for row in rows), default=0)
    embed = discord.Embed(
        title=f"{view.title} — enrollment history",
        description=(
            f"```\n{_sparkline(counts, capacity)}\n```\n"
            f"{len(rows)} observations from "
            f"{rows[0][0]:%b %d} to {rows[-1][0]:%b %d}"
        ),
        colour=discord.Colour.blue(),
        url=view.url,
    )
    embed.add_field(name="Now", value=f"{counts[-1]}/{capacity}", inline=True)
    embed.add_field(name="Peak", value=str(max(counts)), inline=True)
    embed.add_field(name="Low", value=str(min(counts)), inline=True)

    transitions = [
        f"{row[0]:%b %d %H:%M} · {row[3]} ({row[1]}/{row[2]})"
        for previous, row in itertools.pairwise(rows)
        if previous[3] != row[3]
    ]
    if transitions:
        embed.add_field(
            name="Status changes",
            value="\n".join(transitions[-6:]),
            inline=False,
        )
    return embed


_BLOCKS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[int], ceiling: int, width: int = 48) -> str:
    """A fixed-width unicode sparkline; avoids shipping a plotting dependency."""
    if not values:
        return ""
    # Downsample by averaging so the shape survives long series.
    if len(values) > width:
        bucket = len(values) / width
        values = [
            round(
                sum(values[int(i * bucket) : max(int((i + 1) * bucket), int(i * bucket) + 1)])
                / max(
                    1,
                    len(values[int(i * bucket) : max(int((i + 1) * bucket), int(i * bucket) + 1)]),
                )
            )
            for i in range(width)
        ]
    top = max(ceiling, max(values), 1)
    return "".join(_BLOCKS[min(len(_BLOCKS) - 1, v * (len(_BLOCKS) - 1) // top)] for v in values)


async def setup(bot: BruinWatchBot) -> None:
    await bot.add_cog(Search(bot))
