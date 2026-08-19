"""Discord embed rendering."""

from __future__ import annotations

import discord

from ..db.repo import SectionView

STATUS_COLOURS = {
    "Open": discord.Colour.green(),
    "Waitlist": discord.Colour.gold(),
    "Full": discord.Colour.red(),
    "Closed": discord.Colour.red(),
    "Cancelled": discord.Colour.dark_grey(),
    "Tentative": discord.Colour.blurple(),
    "Unknown": discord.Colour.light_grey(),
}

STATUS_EMOJI = {
    "Open": "\N{LARGE GREEN CIRCLE}",
    "Waitlist": "\N{LARGE YELLOW CIRCLE}",
    "Full": "\N{LARGE RED CIRCLE}",
    "Closed": "\N{LARGE RED CIRCLE}",
    "Cancelled": "\N{CROSS MARK}",
    "Tentative": "\N{WHITE QUESTION MARK ORNAMENT}",
    "Unknown": "\N{MEDIUM WHITE CIRCLE}",
}

_BLANK = "​"


def _multiline(values: tuple[str, ...]) -> str:
    return "\n".join(values) if values else "N/A"


def section_embed(view: SectionView, *, choice: str | None = None) -> discord.Embed:
    """Render one section."""
    prefix = f"[{choice}] " if choice else ""
    watched = "  \N{EYES}" if view.watched else ""
    emoji = STATUS_EMOJI.get(view.enrollment_status, "")

    embed = discord.Embed(
        title=f"{prefix}{emoji} {view.title}{watched}",
        description=f"**{view.course_title}**\n[Open on MyUCLA]({view.url})",
        colour=STATUS_COLOURS.get(view.enrollment_status, discord.Colour.light_grey()),
    )
    embed.add_field(name="Term", value=view.term_code, inline=True)
    embed.add_field(name="Units", value=view.units or "N/A", inline=True)
    embed.add_field(name="Days", value=_multiline(view.days), inline=True)
    embed.add_field(name="Times", value=_multiline(view.times), inline=True)
    embed.add_field(name="Locations", value=_multiline(view.locations), inline=True)
    embed.add_field(name="Instructors", value=_multiline(view.instructors), inline=True)

    enrollment = f"{view.enrollment_status} — {view.enrollment_count}/{view.enrollment_capacity}"
    if view.enrollment_status == "Open":
        enrollment += f" ({view.spots_left} left)"
    embed.add_field(name="Enrollment", value=enrollment, inline=True)
    embed.add_field(
        name="Waitlist",
        value=f"{view.waitlist_status} — {view.waitlist_count}/{view.waitlist_capacity}",
        inline=True,
    )
    embed.add_field(name=_BLANK, value=_BLANK, inline=True)

    if view.website:
        embed.add_field(name="Class website", value=view.website, inline=False)
    return embed


def compact_line(view: SectionView) -> str:
    """One-line summary, for lists where a full embed would be too much."""
    emoji = STATUS_EMOJI.get(view.enrollment_status, "")
    watched = " \N{EYES}" if view.watched else ""
    counts = f"{view.enrollment_count}/{view.enrollment_capacity}"
    return f"{emoji} **{view.title}** — {view.enrollment_status} ({counts}){watched}"


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="Sorry", description=message, colour=discord.Colour.dark_red())


def info_embed(title: str, message: str) -> discord.Embed:
    return discord.Embed(title=title, description=message, colour=discord.Colour.blurple())


def about_embed() -> discord.Embed:
    embed = discord.Embed(
        title="BruinWatch",
        description=(
            "Look up UCLA classes and get a DM the moment a section you're "
            "watching changes enrollment status."
        ),
        colour=discord.Colour.blue(),
    )
    embed.add_field(
        name="Find a class",
        value="`/search subject:COM SCI number:32`",
        inline=False,
    )
    embed.add_field(
        name="Watch it",
        value="Pick a section from the dropdown `/search` gives you.",
        inline=False,
    )
    embed.add_field(
        name="Manage your watchlist",
        value="`/watchlist` to review, remove, or set a spots-left alert.",
        inline=False,
    )
    embed.add_field(
        name="See the trend",
        value="`/history` plots a section's enrollment over time.",
        inline=False,
    )
    embed.add_field(
        name="Shorthand",
        value="`/alias set alias:CS target:COM SCI` so you can type `CS` instead.",
        inline=False,
    )
    embed.set_footer(text="Data scraped from the UCLA Registrar's public Schedule of Classes.")
    return embed
