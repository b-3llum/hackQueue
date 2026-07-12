"""Button-based embed paginator for leaderboards."""

from __future__ import annotations

import contextlib

import discord


class Paginator(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], author_id: int, timeout: float = 180) -> None:
        super().__init__(timeout=timeout)
        self._pages = pages
        self._author_id = author_id
        self._index = 0
        self.message: discord.Message | None = None
        if len(pages) <= 1:
            self.clear_items()

    @property
    def current(self) -> discord.Embed:
        return self._pages[self._index]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "Run the command yourself to flip pages.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message is not None:
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._index = (self._index - 1) % len(self._pages)
        await interaction.response.edit_message(embed=self.current, view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._index = (self._index + 1) % len(self._pages)
        await interaction.response.edit_message(embed=self.current, view=self)
