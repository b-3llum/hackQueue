"""/leaderboard — per-platform, claims, and composite boards with pagination."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from hackqueue.adapters.base import PLATFORM_LABELS, Platform
from hackqueue.services.scoring import Period
from hackqueue.ui.embeds import PERIOD_LABELS, board_pages, platform_label
from hackqueue.ui.pagination import Paginator

if TYPE_CHECKING:
    from hackqueue.bot import HackQueueBot

BOARD_CHOICES = [
    app_commands.Choice(name="Composite (all platforms)", value="composite"),
    *[
        app_commands.Choice(name=label, value=platform.value)
        for platform, label in PLATFORM_LABELS.items()
    ],
    app_commands.Choice(name="Manual claims (PG…)", value="claims"),
]
PERIOD_CHOICES = [
    app_commands.Choice(name="Weekly (default)", value="weekly"),
    app_commands.Choice(name="Monthly", value="monthly"),
    app_commands.Choice(name="All-time", value="alltime"),
]


class LeaderboardCog(commands.Cog):
    def __init__(self, bot: HackQueueBot) -> None:
        self.bot = bot

    @app_commands.command(description="Show a server leaderboard")
    @app_commands.describe(board="Which board (default: composite)", period="Time window")
    @app_commands.choices(board=BOARD_CHOICES, period=PERIOD_CHOICES)
    @app_commands.guild_only()
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        board: app_commands.Choice[str] | None = None,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        board_key = board.value if board else "composite"
        period_key = Period(period.value if period else "weekly")
        await interaction.response.defer()

        if board_key == "composite":
            data = await self.bot.boards.composite_board(interaction.guild_id, period_key)
            title = f"🏆 Composite leaderboard — {PERIOD_LABELS[period_key].lower()}"
            pages = board_pages(data, title=title)
        elif board_key == "claims":
            data = await self.bot.boards.claims_board(interaction.guild_id, period_key)
            title = f"🏆 Claims leaderboard — {PERIOD_LABELS[period_key].lower()}"
            pages = board_pages(data, title=title, value_suffix=" pts")
        else:
            platform = Platform(board_key)
            if platform not in self.bot.adapters:
                await interaction.followup.send(
                    f"{platform_label(board_key)} isn't enabled on this instance "
                    "(the operator hasn't configured its API credential)."
                )
                return
            data = await self.bot.boards.platform_board(interaction.guild_id, platform, period_key)
            gained = "" if period_key is Period.ALLTIME else " gained"
            title = (
                f"🏆 {platform_label(board_key)} leaderboard — "
                f"{PERIOD_LABELS[period_key].lower()} (points{gained})"
            )
            pages = board_pages(data, title=title, value_suffix=" pts")

        view = Paginator(pages, author_id=interaction.user.id)
        message = await interaction.followup.send(embed=pages[0], view=view, wait=True)
        view.message = message


async def setup(bot: HackQueueBot) -> None:
    await bot.add_cog(LeaderboardCog(bot))
