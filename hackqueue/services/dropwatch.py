"""Announces HTB's weekly seasonal machine drop.

Checks the active season's live machine on a schedule; when a new one appears
(HTB drops one every Saturday 19:00 UTC), it posts to each guild's announce
channel — once per machine, tracked in the kv store so a restart can't double-post.
"""

from __future__ import annotations

import asyncio
import random

import discord
from sqlalchemy import select

from hackqueue.db.models import Guild
from hackqueue.db.repo import kv_get, kv_set
from hackqueue.db.session import Database
from hackqueue.log import get_logger
from hackqueue.services.seasons import Season, SeasonService
from hackqueue.ui.embeds import season_drop_embed

log = get_logger(__name__)

KV_LAST_DROP = "seasons:last_drop_machine_id"
CHECK_INTERVAL_SECONDS = 1800  # HTB drops weekly; half-hourly catches it promptly


class DropWatchService:
    def __init__(self, client: discord.Client, db: Database, seasons: SeasonService) -> None:
        self._client = client
        self._db = db
        self._seasons = seasons
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="seasons:dropwatch")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(random.uniform(30, 90))
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dropwatch_tick_crashed")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS * random.uniform(0.9, 1.1))

    async def _tick(self) -> None:
        season = await self._seasons.current()
        if season is None or season.live_machine is None:
            return
        machine_id = season.live_machine.machine_id

        async with self._db.session() as session:
            last = await kv_get(session, KV_LAST_DROP) or {}
        if last.get("id") == machine_id:
            return  # already announced this one

        # First run on a fresh DB shouldn't spam-announce the current machine as
        # if it were brand new — record it silently and only announce the NEXT drop.
        first_run = "id" not in last
        async with self._db.session() as session, session.begin():
            await kv_set(session, KV_LAST_DROP, {"id": machine_id})
        if first_run:
            log.info("dropwatch_primed", machine=season.live_machine.name)
            return

        await self._announce(season)

    async def _announce(self, season: Season) -> None:
        embed = season_drop_embed(season)
        async with self._db.session() as session:
            guilds = list(
                await session.scalars(select(Guild).where(Guild.announce_channel_id.is_not(None)))
            )
        posted = 0
        for guild in guilds:
            channel = self._client.get_channel(guild.announce_channel_id or 0)
            if not isinstance(channel, discord.abc.Messageable):
                continue
            try:
                await channel.send(embed=embed)
                posted += 1
            except discord.HTTPException:
                log.warning("dropwatch_post_failed", guild_id=guild.guild_id)
        log.info(
            "season_drop_announced",
            machine=season.live_machine.name if season.live_machine else "?",
            guilds=posted,
        )
