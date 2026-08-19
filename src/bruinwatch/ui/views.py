"""Interactive components.

These replace the old emoji-reaction flows, which needed `bot.wait_for` with no
timeout inside `while True` loops, re-added reactions on every pass, and had no
way to tell one user's menu from another's.

Every view here is owned by a single user, expires on its own, and disables its
own components when it does.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any, cast

import discord

from ..db.repo import SectionView
from .embeds import STATUS_EMOJI, compact_line, section_embed

DEFAULT_TIMEOUT = 180.0


class OwnedView(discord.ui.View):
    """A view only its invoker can operate, which tidies up after itself."""

    def __init__(self, user_id: int, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.message: discord.Message | discord.InteractionMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "That menu belongs to someone else — run the command yourself.", ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button | discord.ui.Select):
                child.disabled = True
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)


#: Called with (interaction, picker, section); returns the new watched state.
ToggleHandler = Callable[[discord.Interaction, "SectionPicker", SectionView], Awaitable[bool]]


class SectionPicker(OwnedView):
    """Dropdown of a course's sections, with a watch/unwatch toggle.

    The picker owns its own watched-state so a second click on the same option
    toggles back, rather than repeating the first action.
    """

    def __init__(
        self,
        user_id: int,
        sections: list[SectionView],
        on_toggle: ToggleHandler,
    ) -> None:
        super().__init__(user_id)
        # Discord caps a select at 25 options; keep the model and the widget
        # in agreement about which sections are actually selectable.
        self.truncated = len(sections) > 25
        self.sections = list(sections[:25])
        self._on_toggle = on_toggle
        self._refresh_options()
        if self.truncated:
            self._widget.placeholder = f"Choose a section (showing 25 of {len(sections)})"

    @property
    def _widget(self) -> discord.ui.Select[Any]:
        """The live Select. ``@discord.ui.select`` swaps the method for one at
        runtime, but type checkers still see the coroutine."""
        return cast("discord.ui.Select[Any]", self.select)

    def _refresh_options(self) -> None:
        self._widget.options = [
            discord.SelectOption(
                label=f"{view.section_label} — {view.enrollment_status}"[:100],
                description=(
                    f"{view.enrollment_count}/{view.enrollment_capacity} enrolled · "
                    f"{', '.join(view.instructors) or 'Staff'}"
                )[:100],
                value=str(view.section_id),
                emoji=STATUS_EMOJI.get(view.enrollment_status),
                default=view.watched,
            )
            for view in self.sections
        ]

    def set_watched(self, section_id: int, watched: bool) -> None:
        """Record the new state and re-render the dropdown's checkmarks."""
        self.sections = [
            dataclasses.replace(s, watched=watched) if s.section_id == section_id else s
            for s in self.sections
        ]
        self._refresh_options()

    @discord.ui.select(
        placeholder="Choose a section to watch or unwatch", min_values=1, max_values=1
    )
    async def select(
        self, interaction: discord.Interaction, select: discord.ui.Select[Any]
    ) -> None:
        section_id = int(select.values[0])
        chosen = next((s for s in self.sections if s.section_id == section_id), None)
        if chosen is None:
            await interaction.response.send_message(
                "That section is no longer available.", ephemeral=True
            )
            return
        watched = await self._on_toggle(interaction, self, chosen)
        self.set_watched(section_id, watched)
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)


class Paginator(OwnedView):
    """Button pagination over pre-rendered pages."""

    def __init__(
        self,
        user_id: int,
        pages: list[discord.Embed],
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(user_id, timeout=timeout)
        if not pages:
            raise ValueError("Paginator needs at least one page")
        self.pages = pages
        self.index = 0
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.first.disabled = self.previous.disabled = self.index == 0
        self.next.disabled = self.last.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(emoji="\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}")
    async def first(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(emoji="\N{LEFTWARDS BLACK ARROW}")
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="1/1", disabled=True, style=discord.ButtonStyle.secondary)
    async def counter(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        """Non-interactive page indicator."""

    @discord.ui.button(emoji="\N{BLACK RIGHTWARDS ARROW}")
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)

    @discord.ui.button(emoji="\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}")
    async def last(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.index = len(self.pages) - 1
        await self._show(interaction)


class WatchlistView(OwnedView):
    """The watchlist, with inline remove and clear-all."""

    def __init__(
        self,
        user_id: int,
        sections: list[SectionView],
        on_remove: Callable[[discord.Interaction, SectionView], Awaitable[None]],
        on_clear: Callable[[discord.Interaction], Awaitable[None]],
    ) -> None:
        super().__init__(user_id)
        self.sections = sections
        self._on_remove = on_remove
        self._on_clear = on_clear
        cast("discord.ui.Select[Any]", self.remove).options = [
            discord.SelectOption(
                label=view.title[:100],
                description=f"{view.term_code} · {view.enrollment_status}"[:100],
                value=str(view.section_id),
                emoji=STATUS_EMOJI.get(view.enrollment_status),
            )
            for view in sections[:25]
        ]

    def render(self) -> discord.Embed:
        body = "\n".join(compact_line(view) for view in self.sections)
        return discord.Embed(
            title=f"Your watchlist ({len(self.sections)})",
            description=body or "Nothing here yet — try `/search`.",
            colour=discord.Colour.blue(),
        )

    @discord.ui.select(placeholder="Remove a section", min_values=1, max_values=1)
    async def remove(
        self, interaction: discord.Interaction, select: discord.ui.Select[Any]
    ) -> None:
        section_id = int(select.values[0])
        chosen = next((s for s in self.sections if s.section_id == section_id), None)
        if chosen is None:
            await interaction.response.send_message("Already removed.", ephemeral=True)
            return
        await self._on_remove(interaction, chosen)

    @discord.ui.button(label="Clear all", style=discord.ButtonStyle.danger, row=1)
    async def clear(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self._on_clear(interaction)


def section_pages(sections: list[SectionView], per_page: int = 1) -> list[discord.Embed]:
    """Chunk sections into embeds for the paginator."""
    if per_page == 1:
        return [section_embed(view) for view in sections]

    pages = []
    for start in range(0, len(sections), per_page):
        chunk = sections[start : start + per_page]
        pages.append(
            discord.Embed(
                title=f"Sections {start + 1}–{start + len(chunk)} of {len(sections)}",
                description="\n".join(compact_line(view) for view in chunk),
                colour=discord.Colour.blue(),
            )
        )
    return pages
