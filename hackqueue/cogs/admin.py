"""/config group and /health — per-guild configuration and instance status."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from hackqueue.adapters.base import PLATFORM_LABELS, Platform
from hackqueue.db.models import AccountLink, CatalogBox, Snapshot, Solve
from hackqueue.db.repo import ensure_guild
from hackqueue.ui.embeds import COLOR_INFO, health_embed

if TYPE_CHECKING:
    from hackqueue.bot import HackQueueBot

PLATFORM_CHOICES = [
    app_commands.Choice(name=label, value=platform.value)
    for platform, label in PLATFORM_LABELS.items()
]


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class ConfigGroup(app_commands.Group, name="config", description="Configure hackQueue"):
    def __init__(self, bot: HackQueueBot) -> None:
        super().__init__()
        self.bot = bot

    async def _update(self, guild_id: int, **fields: object) -> None:
        async with self.bot.db.session() as session, session.begin():
            guild = await ensure_guild(session, guild_id)
            for key, value in fields.items():
                setattr(guild, key, value)

    @app_commands.command(description="Show the current server configuration")
    async def show(self, interaction: discord.Interaction) -> None:
        async with self.bot.db.session() as session:
            guild = await ensure_guild(session, interaction.guild_id)
            await session.commit()
        embed = discord.Embed(title="Server configuration", color=COLOR_INFO)
        embed.add_field(
            name="Moderator role",
            value=f"<@&{guild.mod_role_id}>" if guild.mod_role_id else "not set",
        )
        embed.add_field(
            name="Claims mod channel",
            value=f"<#{guild.mod_channel_id}>" if guild.mod_channel_id else "not set",
        )
        embed.add_field(
            name="Recap channel",
            value=f"<#{guild.recap_channel_id}>" if guild.recap_channel_id else "not set",
        )
        embed.add_field(name="Weekly recap", value="on" if guild.recap_enabled else "off")
        embed.add_field(
            name="Require verified links", value="on" if guild.require_verified else "off"
        )
        web = "off"
        if guild.web_enabled:
            base = self.bot.settings.web_base_url.rstrip("/")
            web = f"[published]({base}/g/{guild.guild_id})"
        embed.add_field(name="Web leaderboard", value=web)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="mod-role", description="Role allowed to review claims")
    async def mod_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self._update(interaction.guild_id, mod_role_id=role.id)
        await interaction.response.send_message(
            f"✅ Claim reviewers: {role.mention}", ephemeral=True
        )

    @app_commands.command(name="mod-channel", description="Channel where claims are reviewed")
    async def mod_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self._update(interaction.guild_id, mod_channel_id=channel.id)
        await interaction.response.send_message(
            f"✅ Claims will be posted to {channel.mention}", ephemeral=True
        )

    @app_commands.command(name="recap-channel", description="Channel for the weekly recap post")
    async def recap_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self._update(interaction.guild_id, recap_channel_id=channel.id, recap_enabled=True)
        await interaction.response.send_message(
            f"✅ Weekly recap enabled in {channel.mention} (Mondays)", ephemeral=True
        )

    @app_commands.command(name="recap", description="Turn the weekly recap on or off")
    async def recap(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._update(interaction.guild_id, recap_enabled=enabled)
        await interaction.response.send_message(
            f"✅ Weekly recap {'enabled' if enabled else 'disabled'}", ephemeral=True
        )

    @app_commands.command(
        name="require-verified",
        description="Hide unverified links from leaderboards",
    )
    async def require_verified(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._update(interaction.guild_id, require_verified=enabled)
        await interaction.response.send_message(
            f"✅ Unverified links are now {'hidden from' if enabled else 'shown on'} boards "
            f"{'' if enabled else '(marked with ⚠)'}",
            ephemeral=True,
        )

    @app_commands.command(
        name="unlink-member",
        description="Admin override: unlink a member's platform account (purges their data)",
    )
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def unlink_member(
        self,
        interaction: discord.Interaction,
        member: discord.User,
        platform: app_commands.Choice[str],
    ) -> None:
        purged = await self.bot.linking.unlink(member.id, Platform(platform.value))
        message = (
            f"🗑️ Unlinked {platform.name} for {member.mention}; their data was purged."
            if purged
            else f"{member.mention} has no {platform.name} link."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        name="web", description="Publish this server's leaderboard as a web page, or unpublish it"
    )
    @app_commands.describe(enabled="On publishes the board at a public URL; off takes it down")
    async def web(self, interaction: discord.Interaction, enabled: bool) -> None:
        if enabled and not self.bot.settings.web_enabled:
            await interaction.response.send_message(
                "❌ The web board is switched off on this hackQueue instance. "
                "The operator enables it by setting `WEB_ENABLED=true`.",
                ephemeral=True,
            )
            return
        await self._update(interaction.guild_id, web_enabled=enabled)
        if not enabled:
            await interaction.response.send_message(
                "🔒 Web leaderboard unpublished — the page now 404s.", ephemeral=True
            )
            return
        url = f"{self.bot.settings.web_base_url.rstrip('/')}/g/{interaction.guild_id}"
        await interaction.response.send_message(
            f"🌐 Leaderboard published: {url}\n"
            "-# Anyone with the link can see participating members' Discord display names, "
            "avatars and scores. `/config web off` takes it down.",
            ephemeral=True,
        )

    @app_commands.command(
        name="purge-member",
        description="Delete a member's manual claims in this server (incl. proof links)",
    )
    async def purge_member(self, interaction: discord.Interaction, member: discord.User) -> None:
        count = await self.bot.claims.purge_user(interaction.guild_id, member.id)
        await interaction.response.send_message(
            f"🗑️ Deleted {count} claim(s) by {member.mention} in this server. "
            "Platform links are removed separately via `/config unlink-member`.",
            ephemeral=True,
        )


class AdminCog(commands.Cog):
    def __init__(self, bot: HackQueueBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.tree.add_command(ConfigGroup(self.bot))

    @app_commands.command(description="Platform health and instance stats (admins)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def health(self, interaction: discord.Interaction) -> None:
        async with self.bot.db.session() as session:
            counts = {
                "links": await session.scalar(select(func.count()).select_from(AccountLink)),
                "snapshots": await session.scalar(select(func.count()).select_from(Snapshot)),
                "solves": await session.scalar(select(func.count()).select_from(Solve)),
                "boxes": await session.scalar(select(func.count()).select_from(CatalogBox)),
            }
        embed = health_embed(self.bot.health, self.bot.adapters.platforms, counts)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: HackQueueBot) -> None:
    await bot.add_cog(AdminCog(bot))
