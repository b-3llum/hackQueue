"""/season — the active HTB season, this week's drop, and the server's race
through the season's boxes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from hackqueue.adapters.base import AdapterError
from hackqueue.http.client import NETWORK_ERRORS
from hackqueue.ui.embeds import season_embed

if TYPE_CHECKING:
    from hackqueue.bot import HackQueueBot


class SeasonsCog(commands.Cog):
    def __init__(self, bot: HackQueueBot) -> None:
        self.bot = bot

    @app_commands.command(
        description="The current Hack The Box season, this week's box, and the server standings"
    )
    @app_commands.guild_only()
    async def season(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            season = await self.bot.seasons.current()
        except (AdapterError, *NETWORK_ERRORS):
            await interaction.followup.send(
                "⚠ Couldn't reach Hack The Box for season data right now — try again shortly."
            )
            return
        if season is None:
            await interaction.followup.send(
                "No Hack The Box season is running right now (or HTB isn't configured "
                "on this instance)."
            )
            return
        standings = await self.bot.seasons.standings(interaction.guild_id, season)
        guild_name = interaction.guild.name if interaction.guild else "This server"
        await interaction.followup.send(embed=season_embed(season, standings, guild_name))


async def setup(bot: HackQueueBot) -> None:
    await bot.add_cog(SeasonsCog(bot))
