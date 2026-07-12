"""/solved — manual claims with moderator approve/deny buttons.

The buttons are DynamicItems keyed by custom_id, so they keep working across
bot restarts without any persistent-view bookkeeping.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from hackqueue.db.repo import ensure_guild
from hackqueue.services.claims import ClaimError
from hackqueue.ui.embeds import claim_embed

if TYPE_CHECKING:
    from hackqueue.bot import HackQueueBot


async def _is_moderator(interaction: discord.Interaction) -> bool:
    bot = cast("HackQueueBot", interaction.client)
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    async with bot.db.session() as session:
        guild = await ensure_guild(session, interaction.guild_id)
        mod_role_id = guild.mod_role_id
    return mod_role_id is not None and member.get_role(mod_role_id) is not None


class ClaimAction(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"hq:claim:(?P<action>approve|deny):(?P<claim_id>\d+)",
):
    def __init__(self, action: str, claim_id: int) -> None:
        self.action = action
        self.claim_id = claim_id
        super().__init__(
            discord.ui.Button(
                label=action.title(),
                style=discord.ButtonStyle.success
                if action == "approve"
                else discord.ButtonStyle.danger,
                custom_id=f"hq:claim:{action}:{claim_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> ClaimAction:
        return cls(match["action"], int(match["claim_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("HackQueueBot", interaction.client)
        if not await _is_moderator(interaction):
            await interaction.response.send_message(
                "Only moderators can review claims (configure the role with `/config mod-role`).",
                ephemeral=True,
            )
            return
        claim = await bot.claims.review(
            self.claim_id, approve=self.action == "approve", reviewer_id=interaction.user.id
        )
        if claim is None:
            await interaction.response.send_message(
                "This claim was already reviewed.", ephemeral=True
            )
            return
        cfg = bot.claims.platform(claim.platform_key)
        embed = claim_embed(claim, cfg.name if cfg else claim.platform_key)
        await interaction.response.edit_message(embed=embed, view=None)
        if claim.status == "approved":
            note = (
                f"✅ <@{claim.discord_user_id}>'s claim for **{claim.item_name}** approved "
                f"(+{claim.points} pts)."
            )
        else:
            note = f"❌ <@{claim.discord_user_id}>'s claim for **{claim.item_name}** was denied."
        await interaction.followup.send(note)


class ClaimsCog(commands.Cog):
    def __init__(self, bot: HackQueueBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ClaimAction)

    @app_commands.command(
        name="solved",
        description="Claim a solve on a platform without an API (Proving Grounds…)",
    )
    @app_commands.describe(
        platform="Claim platform",
        name="The box/lab you solved",
        difficulty="Its difficulty (determines points)",
        proof="Optional proof screenshot",
    )
    @app_commands.guild_only()
    async def solved(
        self,
        interaction: discord.Interaction,
        platform: str,
        name: str,
        difficulty: str,
        proof: discord.Attachment | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as session:
            guild = await ensure_guild(session, interaction.guild_id)
            mod_channel_id = guild.mod_channel_id
        if mod_channel_id is None:
            await interaction.followup.send(
                "❌ This server has no moderation channel configured for claims — "
                "ask an admin to run `/config mod-channel`.",
                ephemeral=True,
            )
            return
        channel = interaction.guild.get_channel(mod_channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ The configured moderation channel no longer exists — "
                "ask an admin to re-run `/config mod-channel`.",
                ephemeral=True,
            )
            return
        try:
            claim = await self.bot.claims.create(
                interaction.guild_id,
                interaction.user.id,
                platform,
                name,
                difficulty,
                proof.url if proof else None,
            )
        except ClaimError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        cfg = self.bot.claims.platform(platform)
        view = discord.ui.View(timeout=None)
        view.add_item(ClaimAction("approve", claim.id))
        view.add_item(ClaimAction("deny", claim.id))
        message = await channel.send(
            embed=claim_embed(claim, cfg.name if cfg else platform), view=view
        )
        await self.bot.claims.set_message(claim.id, message.id)
        await interaction.followup.send(
            f"📨 Claim for **{claim.item_name}** ({claim.difficulty}, {claim.points} pts) "
            "submitted for moderator review.",
            ephemeral=True,
        )

    @solved.autocomplete("platform")
    async def _platform_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=cfg.name, value=key)
            for key, cfg in self.bot.claims.platforms.items()
            if current.lower() in key or current.lower() in cfg.name.lower()
        ][:25]

    @solved.autocomplete("difficulty")
    async def _difficulty_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        platform = interaction.namespace.platform
        cfg = self.bot.claims.platform(platform) if platform else None
        difficulties = sorted(cfg.points) if cfg else []
        return [
            app_commands.Choice(name=f"{d.title()} ({cfg.points[d]} pts)", value=d)
            for d in difficulties
            if current.lower() in d
        ][:25]


async def setup(bot: HackQueueBot) -> None:
    await bot.add_cog(ClaimsCog(bot))
