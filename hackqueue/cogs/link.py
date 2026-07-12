"""/link, /unlink, /verify, /rootme-search — account linking commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from hackqueue.adapters.base import PLATFORM_LABELS, AdapterError, Platform
from hackqueue.adapters.rootme import RootMeAdapter
from hackqueue.http.client import NETWORK_ERRORS
from hackqueue.services.linking import LinkError

if TYPE_CHECKING:
    from hackqueue.bot import HackQueueBot

PLATFORM_CHOICES = [
    app_commands.Choice(name=label, value=platform.value)
    for platform, label in PLATFORM_LABELS.items()
]


class LinkCog(commands.Cog):
    def __init__(self, bot: HackQueueBot) -> None:
        self.bot = bot

    @app_commands.command(description="Link a CTF platform account to your Discord user")
    @app_commands.describe(platform="The platform to link", account="Your ID or username there")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    @app_commands.guild_only()
    async def link(
        self, interaction: discord.Interaction, platform: app_commands.Choice[str], account: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        plat = Platform(platform.value)
        try:
            link = await self.bot.linking.link(
                interaction.guild_id, interaction.user.id, plat, account
            )
        except (LinkError, AdapterError) as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except NETWORK_ERRORS:
            await interaction.followup.send(
                f"⚠ {platform.name} couldn't be reached right now — try again in a bit.",
                ephemeral=True,
            )
            return
        adapter = self.bot.adapters.get(plat)
        verify_hint = (
            f"\nTip: `/verify {plat.value}` proves account ownership and removes the ⚠ marker."
            if adapter and adapter.supports_verification
            else ""
        )
        await interaction.followup.send(
            f"✅ Linked **{link.platform_username}** on {platform.name}. "
            f"Your stats will appear on the boards after the next poll.{verify_hint}\n"
            f"-# hackQueue stores your Discord ID, this account reference, and score "
            f"snapshots. `/unlink {plat.value}` deletes all of it.",
            ephemeral=True,
        )

    @app_commands.command(description="Unlink a platform account and delete all its stored data")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def unlink(
        self, interaction: discord.Interaction, platform: app_commands.Choice[str]
    ) -> None:
        purged = await self.bot.linking.unlink(interaction.user.id, Platform(platform.value))
        if purged:
            message = (
                f"🗑️ Unlinked {platform.name}. All snapshots and solve history "
                "for that account have been deleted."
            )
        else:
            message = f"You had no {platform.name} account linked."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        description="Verify you own your linked account by placing a token in its profile bio"
    )
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def verify(
        self, interaction: discord.Interaction, platform: app_commands.Choice[str]
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            phase, token = await self.bot.linking.start_or_check_verification(
                interaction.user.id, Platform(platform.value)
            )
        except (LinkError, AdapterError) as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except NETWORK_ERRORS:
            await interaction.followup.send(
                f"⚠ {platform.name} couldn't be reached right now — try again in a bit.",
                ephemeral=True,
            )
            return
        if phase == "issued":
            message = (
                f"🔑 Your verification token: `{token}`\n"
                f"1. Put it anywhere in your **public** {platform.name} profile description/bio.\n"
                f"2. Run `/verify {platform.value}` again.\n"
                "The token expires in 24 h. You can remove it from your bio once verified."
            )
        elif phase == "verified":
            message = "✅ Verified! Your link now shows without the ⚠ marker."
        else:
            message = (
                f"❌ Token `{token}` not found in your {platform.name} bio yet. "
                "Save your profile and try again (give it a minute to propagate)."
            )
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(
        name="rootme-search", description="Find your Root-Me author ID by nickname"
    )
    async def rootme_search(self, interaction: discord.Interaction, name: str) -> None:
        adapter = self.bot.adapters.get(Platform.ROOTME)
        if not isinstance(adapter, RootMeAdapter):
            await interaction.response.send_message(
                "Root-Me isn't enabled on this instance.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            matches = await adapter.search_by_name(name)
        except AdapterError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except NETWORK_ERRORS:
            await interaction.followup.send(
                "⚠ Root-Me couldn't be reached right now — try again in a bit.",
                ephemeral=True,
            )
            return
        if not matches:
            await interaction.followup.send(f"No Root-Me users match **{name}**.", ephemeral=True)
            return
        lines = [f"• **{nom}** — ID `{author_id}`" for author_id, nom in matches[:10]]
        await interaction.followup.send(
            "Matching Root-Me accounts (link with `/link rootme <ID>`):\n" + "\n".join(lines),
            ephemeral=True,
        )


async def setup(bot: HackQueueBot) -> None:
    await bot.add_cog(LinkCog(bot))
