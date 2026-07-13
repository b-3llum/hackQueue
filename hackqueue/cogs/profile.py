"""/profile — all linked accounts, current stats, and recent solves."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from hackqueue.db.models import Claim, Snapshot, Solve
from hackqueue.ui.embeds import profile_embed

if TYPE_CHECKING:
    from hackqueue.bot import HackQueueBot


class ProfileCog(commands.Cog):
    def __init__(self, bot: HackQueueBot) -> None:
        self.bot = bot

    @app_commands.command(description="Show linked accounts and stats for you or another member")
    @app_commands.describe(member="Whose profile to show (default: you)")
    @app_commands.guild_only()
    async def profile(
        self, interaction: discord.Interaction, member: discord.User | None = None
    ) -> None:
        target = member or interaction.user
        await interaction.response.defer()
        await self.bot.directory.remember(interaction.guild_id, target)
        links = await self.bot.linking.links_of(target.id)
        latest: dict[int, Snapshot | None] = {}
        async with self.bot.db.session() as session:
            for link in links:
                latest[link.id] = await session.scalar(
                    select(Snapshot)
                    .where(Snapshot.link_id == link.id)
                    .order_by(Snapshot.taken_at.desc())
                    .limit(1)
                )
            recent = []
            if links:
                recent = list(
                    await session.scalars(
                        select(Solve)
                        .where(Solve.link_id.in_([link.id for link in links]))
                        .order_by(Solve.first_seen_at.desc())
                        .limit(5)
                    )
                )
            approved = await session.scalar(
                select(func.count()).where(
                    Claim.guild_id == interaction.guild_id,
                    Claim.discord_user_id == target.id,
                    Claim.status == "approved",
                )
            )
        verifiable = {
            adapter.platform.value for adapter in self.bot.adapters if adapter.supports_verification
        }
        await interaction.followup.send(
            embed=profile_embed(target, links, latest, recent, int(approved or 0), verifiable)
        )


async def setup(bot: HackQueueBot) -> None:
    await bot.add_cog(ProfileCog(bot))
