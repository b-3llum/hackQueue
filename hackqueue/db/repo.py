"""Small shared query helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hackqueue.db.models import KV, Guild, GuildMember, utcnow


async def ensure_guild(session: AsyncSession, guild_id: int) -> Guild:
    guild = await session.get(Guild, guild_id)
    if guild is None:
        guild = Guild(guild_id=guild_id)
        session.add(guild)
        await session.flush()
    return guild


async def ensure_member(session: AsyncSession, guild_id: int, discord_user_id: int) -> GuildMember:
    await ensure_guild(session, guild_id)
    member = await session.get(GuildMember, (guild_id, discord_user_id))
    if member is None:
        member = GuildMember(guild_id=guild_id, discord_user_id=discord_user_id)
        session.add(member)
        await session.flush()
    return member


async def kv_get(session: AsyncSession, key: str) -> dict[str, Any] | None:
    row = await session.get(KV, key)
    return row.value if row else None


async def kv_set(session: AsyncSession, key: str, value: dict[str, Any]) -> None:
    row = await session.get(KV, key)
    if row is None:
        session.add(KV(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()


async def guilds_with_recap(session: AsyncSession) -> list[Guild]:
    result = await session.scalars(
        select(Guild).where(Guild.recap_enabled.is_(True), Guild.recap_channel_id.is_not(None))
    )
    return list(result)
