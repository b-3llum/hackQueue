"""/setup — create the channels hackQueue needs and wire them up in one go."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from hackqueue.db.repo import ensure_guild
from hackqueue.log import get_logger
from hackqueue.ui.embeds import COLOR_OK

if TYPE_CHECKING:
    from hackqueue.bot import HackQueueBot

log = get_logger(__name__)

CATEGORY_NAME = "hackQueue"
LEADERBOARD_CHANNEL = "leaderboard"
CLAIMS_CHANNEL = "claim-review"

LEADERBOARD_TOPIC = "Weekly recaps, new solves and box of the week — posted by hackQueue."
CLAIMS_TOPIC = "Manual solve claims awaiting moderator approval."


class SetupCog(commands.Cog):
    def __init__(self, bot: HackQueueBot) -> None:
        self.bot = bot

    @app_commands.command(
        description="Create hackQueue's channels and configure this server in one step"
    )
    @app_commands.describe(
        mod_role="Role allowed to approve claims (defaults to anyone with Manage Server)"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setup(
        self, interaction: discord.Interaction, mod_role: discord.Role | None = None
    ) -> None:
        guild = interaction.guild
        me = guild.me
        if not me.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "❌ I need the **Manage Channels** permission to create the channels. "
                "Grant it and run `/setup` again — or create them yourself and use "
                "`/config mod-channel` and `/config recap-channel`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        try:
            category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
            if category is None:
                category = await guild.create_category(CATEGORY_NAME, reason="hackQueue setup")

            leaderboard = discord.utils.get(category.text_channels, name=LEADERBOARD_CHANNEL)
            if leaderboard is None:
                leaderboard = await guild.create_text_channel(
                    LEADERBOARD_CHANNEL,
                    category=category,
                    topic=LEADERBOARD_TOPIC,
                    reason="hackQueue setup",
                )

            claims = discord.utils.get(category.text_channels, name=CLAIMS_CHANNEL)
            if claims is None:
                # Claims carry proof screenshots and are moderator business:
                # hide the channel from @everyone, show it to the mod role.
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    me: discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, embed_links=True
                    ),
                }
                if mod_role is not None:
                    overwrites[mod_role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True
                    )
                claims = await guild.create_text_channel(
                    CLAIMS_CHANNEL,
                    category=category,
                    topic=CLAIMS_TOPIC,
                    overwrites=overwrites,
                    reason="hackQueue setup",
                )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Discord refused the channel creation — check that my role sits above "
                "the ones you're granting access to, and that I can manage channels here.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.warning("setup_failed", guild_id=guild.id, error=str(exc))
            await interaction.followup.send(
                f"❌ Discord rejected the setup: {exc.text or exc}", ephemeral=True
            )
            return

        async with self.bot.db.session() as session, session.begin():
            config = await ensure_guild(session, guild.id)
            config.mod_channel_id = claims.id
            config.recap_channel_id = leaderboard.id
            config.announce_channel_id = leaderboard.id
            config.recap_enabled = True
            if mod_role is not None:
                config.mod_role_id = mod_role.id

        embed = discord.Embed(
            title="hackQueue is set up",
            color=COLOR_OK,
            description=(
                f"**{leaderboard.mention}** — weekly recaps and announcements\n"
                f"**{claims.mention}** — claim approvals (moderators only)\n"
                f"Claim reviewers: {mod_role.mention if mod_role else '**Manage Server** holders'}"
            ),
        )
        embed.add_field(
            name="Next",
            value=(
                "• Members run `/link htb <id>` to join the boards\n"
                "• `/config web on` publishes the leaderboard as a web page\n"
                "• `/config show` to review everything"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("guild_setup", guild_id=guild.id)


async def setup(bot: HackQueueBot) -> None:
    await bot.add_cog(SetupCog(bot))
