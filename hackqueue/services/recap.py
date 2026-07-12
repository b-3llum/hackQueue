"""Weekly recap: scheduled digest of top gainers, new solves, and bloods,
posted to each guild's configured recap channel on Mondays."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, timedelta

import discord
from sqlalchemy import func, select

from hackqueue.db.models import AccountLink, Guild, GuildMember, Solve, utcnow
from hackqueue.db.repo import guilds_with_recap, kv_get, kv_set
from hackqueue.db.session import Database
from hackqueue.log import get_logger
from hackqueue.services.boards import BoardService
from hackqueue.services.catalog import CatalogService
from hackqueue.services.scoring import Period, period_start
from hackqueue.ui.embeds import recap_embed

log = get_logger(__name__)

POST_HOUR_UTC = 9  # Mondays, 09:00 UTC
CHECK_INTERVAL_SECONDS = 1800


class RecapService:
    def __init__(
        self,
        client: discord.Client,
        db: Database,
        boards: BoardService,
        catalog: CatalogService,
    ) -> None:
        self._client = client
        self._db = db
        self._boards = boards
        self._catalog = catalog
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="recap:schedule")

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
                log.exception("recap_tick_crashed")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def _tick(self) -> None:
        now = utcnow().astimezone(UTC)
        if now.weekday() != 0 or now.hour < POST_HOUR_UTC:
            return
        week_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        async with self._db.session() as session:
            guilds = await guilds_with_recap(session)
        for guild in guilds:
            await self._post_if_due(guild, week_key)

    async def _post_if_due(self, guild: Guild, week_key: str) -> None:
        kv_key = f"recap:{guild.guild_id}"
        async with self._db.session() as session:
            posted = await kv_get(session, kv_key) or {}
        if posted.get("week") == week_key:
            return
        channel = self._client.get_channel(guild.recap_channel_id or 0)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("recap_channel_missing", guild_id=guild.guild_id)
            return
        embed = await self.build_recap(guild.guild_id)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("recap_post_failed", guild_id=guild.guild_id)
            return
        async with self._db.session() as session, session.begin():
            await kv_set(session, kv_key, {"week": week_key})
        log.info("recap_posted", guild_id=guild.guild_id, week=week_key)

    async def build_recap(self, guild_id: int) -> discord.Embed:
        # The recap covers the COMPLETED week: anchor the board just before
        # this week's Monday 00:00 UTC so deltas span the previous Mon-Sun,
        # and window solve counts to [previous Monday, this Monday).
        this_week = period_start(Period.WEEKLY, utcnow())
        assert this_week is not None
        as_of = this_week - timedelta(seconds=1)
        start = period_start(Period.WEEKLY, as_of)
        assert start is not None
        board = await self._boards.composite_board(guild_id, Period.WEEKLY, as_of=as_of)
        # Backfilled solves (a member's pre-link history imported on their
        # first poll) are not news — exclude them or a Saturday /link floods
        # Monday's recap with years-old solves.
        window = (
            Solve.first_seen_at >= start,
            Solve.first_seen_at < this_week,
            Solve.backfilled.is_(False),
        )
        async with self._db.session() as session:
            solve_counts = {
                platform: int(count)
                for platform, count in await session.execute(
                    select(Solve.platform, func.count())
                    .join(AccountLink, AccountLink.id == Solve.link_id)
                    .join(
                        GuildMember,
                        GuildMember.discord_user_id == AccountLink.discord_user_id,
                    )
                    .where(GuildMember.guild_id == guild_id, *window)
                    .group_by(Solve.platform)
                )
            }
            bloods = list(
                await session.scalars(
                    select(Solve)
                    .join(AccountLink, AccountLink.id == Solve.link_id)
                    .join(
                        GuildMember,
                        GuildMember.discord_user_id == AccountLink.discord_user_id,
                    )
                    .where(
                        GuildMember.guild_id == guild_id,
                        Solve.first_blood.is_(True),
                        *window,
                    )
                )
            )
        box = await self._catalog.box_of_week()
        return recap_embed(board, solve_counts, bloods, box, week_of=start)
