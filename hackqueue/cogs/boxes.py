"""/suggest and /box — box recommendations from the local catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from hackqueue.db.models import CatalogBox
from hackqueue.ui.embeds import box_embed, suggest_embed

if TYPE_CHECKING:
    from hackqueue.bot import HackQueueBot

DIFFICULTY_CHOICES = [
    app_commands.Choice(name=d.title(), value=d) for d in ("easy", "medium", "hard", "insane")
]
OS_CHOICES = [
    app_commands.Choice(name=o, value=o) for o in ("Linux", "Windows", "FreeBSD", "OpenBSD")
]


class BoxesCog(commands.Cog):
    def __init__(self, bot: HackQueueBot) -> None:
        self.bot = bot

    @app_commands.command(description="Suggest boxes you haven't solved yet")
    @app_commands.describe(
        difficulty="Filter by difficulty",
        os="Filter by operating system",
        tag="Filter by tag (e.g. Active Directory)",
        active_only="Only currently-active (non-retired) boxes",
    )
    @app_commands.choices(difficulty=DIFFICULTY_CHOICES, os=OS_CHOICES)
    @app_commands.guild_only()
    async def suggest(
        self,
        interaction: discord.Interaction,
        difficulty: app_commands.Choice[str] | None = None,
        os: app_commands.Choice[str] | None = None,
        tag: str | None = None,
        active_only: bool = False,
    ) -> None:
        await interaction.response.defer()
        owned = await self.bot.catalog.owned_refs(interaction.user.id, "htb")
        boxes = await self.bot.catalog.suggest(
            platform="htb",
            difficulty=difficulty.value if difficulty else None,
            os=os.value if os else None,
            tag=tag,
            exclude_refs=owned,
            include_retired=not active_only,
        )
        embed = suggest_embed(boxes)
        if not boxes and not await self._catalog_ready():
            embed.description = (
                "The box catalog is empty — it syncs from the HTB API and needs "
                "`HTB_APP_TOKEN` to be configured on this instance."
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(description="Show a box's info card with its IppSec walkthrough")
    @app_commands.describe(name="Box name")
    async def box(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer()
        found = await self.bot.catalog.find_box(name)
        if found is None:
            await interaction.followup.send(
                f"No box matching **{name}** in the catalog.", ephemeral=True
            )
            return
        await interaction.followup.send(embed=box_embed(found))

    @box.autocomplete("name")
    async def _box_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not current:
            return []
        async with self.bot.db.session() as session:
            names = await session.scalars(
                select(CatalogBox.name)
                .where(CatalogBox.name.ilike(f"%{current}%"))
                .order_by(CatalogBox.name)
                .limit(25)
            )
            return [app_commands.Choice(name=n, value=n) for n in names]

    async def _catalog_ready(self) -> bool:
        async with self.bot.db.session() as session:
            return await session.scalar(select(CatalogBox.id).limit(1)) is not None


async def setup(bot: HackQueueBot) -> None:
    await bot.add_cog(BoxesCog(bot))
