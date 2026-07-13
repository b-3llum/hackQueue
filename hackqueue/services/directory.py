"""Display names and avatars for the web board.

hackQueue runs without privileged intents, so the member cache is empty and
names must be fetched over HTTP. They're cached in ``guild_members`` and
refreshed lazily (at most a handful per web request) so a public board never
turns into a burst of Discord API calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import discord
from sqlalchemy import select

from hackqueue.db.models import GuildMember, utcnow
from hackqueue.db.session import Database
from hackqueue.log import get_logger

log = get_logger(__name__)

STALE_AFTER = timedelta(days=1)
#: Cap on Discord fetches per refresh, so one page view can't fan out.
MAX_FETCHES_PER_CALL = 10


@dataclass(frozen=True)
class MemberIdentity:
    display_name: str
    avatar_url: str | None


def _identity(member: discord.Member | discord.User) -> MemberIdentity:
    name = getattr(member, "display_name", None) or member.name
    return MemberIdentity(display_name=name[:64], avatar_url=member.display_avatar.url)


class DirectoryService:
    def __init__(self, db: Database, client: discord.Client) -> None:
        self._db = db
        self._client = client

    async def remember(self, guild_id: int, member: discord.Member | discord.User) -> None:
        """Cache what we already know for free — called whenever a member
        interacts with the bot."""
        await self._store(guild_id, member.id, _identity(member))

    async def identities(self, guild_id: int, user_ids: list[int]) -> dict[int, MemberIdentity]:
        """Names/avatars for a board render. Cached rows are returned as-is;
        missing or stale ones are fetched (bounded) and written back."""
        known: dict[int, MemberIdentity] = {}
        stale: list[int] = []
        cutoff = utcnow() - STALE_AFTER
        async with self._db.session() as session:
            rows = await session.scalars(
                select(GuildMember).where(
                    GuildMember.guild_id == guild_id,
                    GuildMember.discord_user_id.in_(user_ids),
                )
            )
            for row in rows:
                if row.display_name:
                    known[row.discord_user_id] = MemberIdentity(
                        display_name=row.display_name, avatar_url=row.avatar_url
                    )
                if row.display_name is None or (
                    row.display_updated_at is None or row.display_updated_at < cutoff
                ):
                    stale.append(row.discord_user_id)

        guild = self._client.get_guild(guild_id)
        for user_id in stale[:MAX_FETCHES_PER_CALL]:
            identity = await self._fetch(guild, user_id)
            if identity is not None:
                known[user_id] = identity
                await self._store(guild_id, user_id, identity)
        return known

    async def _fetch(self, guild: discord.Guild | None, user_id: int) -> MemberIdentity | None:
        if guild is not None and (cached := guild.get_member(user_id)) is not None:
            return _identity(cached)
        try:
            if guild is not None:
                return _identity(await guild.fetch_member(user_id))
            return _identity(await self._client.fetch_user(user_id))
        except discord.NotFound:
            return None  # left the server / deleted account
        except discord.HTTPException:
            log.warning("member_fetch_failed", user_id=user_id)
            return None

    async def _store(self, guild_id: int, user_id: int, identity: MemberIdentity) -> None:
        async with self._db.session() as session, session.begin():
            row = await session.get(GuildMember, (guild_id, user_id))
            if row is None:
                return  # not a board participant; nothing to cache
            row.display_name = identity.display_name
            row.avatar_url = identity.avatar_url
            row.display_updated_at = utcnow()
