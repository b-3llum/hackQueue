"""Leaderboard assembly: joins guild membership with links and snapshots and
feeds the pure math in ``scoring.py``. All queries are per-guild.

Every board accepts an optional ``as_of`` anchor: the period and all data are
evaluated as if the clock read that instant. Live boards use now (the
default); the weekly recap anchors just before Monday 00:00 UTC so it scores
the *completed* week instead of the first hours of the new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hackqueue.adapters.base import Platform
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.config import ScoringConfig
from hackqueue.db.models import AccountLink, Claim, Guild, GuildMember, Snapshot, utcnow
from hackqueue.db.session import Database
from hackqueue.services.health import HealthRegistry
from hackqueue.services.scoring import Period, composite_scores, period_start, points_delta

CLAIMS_KEY = "claims"


@dataclass(frozen=True)
class BoardRow:
    discord_user_id: int
    label: str  # platform username, or empty for composite rows
    value: float
    verified: bool


@dataclass(frozen=True)
class Board:
    rows: list[BoardRow]  # sorted best-first
    period: Period
    #: platforms whose data is stale (degraded/auth error) — shown in the footer
    stale_platforms: list[str]


class BoardService:
    def __init__(
        self,
        db: Database,
        adapters: AdapterRegistry,
        health: HealthRegistry,
        scoring_config: ScoringConfig,
    ) -> None:
        self._db = db
        self._adapters = adapters
        self._health = health
        self._scoring = scoring_config

    async def platform_board(
        self,
        guild_id: int,
        platform: Platform,
        period: Period,
        as_of: datetime | None = None,
    ) -> Board:
        as_of = as_of or utcnow()
        start = period_start(period, as_of)
        async with self._db.session() as session:
            links = await self._guild_links(session, guild_id, platform)
            rows = [
                BoardRow(
                    discord_user_id=link.discord_user_id,
                    label=link.platform_username,
                    value=float(await self._link_delta(session, link.id, start, as_of)),
                    verified=link.verified,
                )
                for link in links
            ]
        rows = [r for r in rows if r.value > 0] if start is not None else rows
        rows.sort(key=lambda r: r.value, reverse=True)
        stale = [platform.value] if self._health.is_stale(platform) else []
        return Board(rows=rows, period=period, stale_platforms=stale)

    async def claims_board(
        self, guild_id: int, period: Period, as_of: datetime | None = None
    ) -> Board:
        as_of = as_of or utcnow()
        start = period_start(period, as_of)
        async with self._db.session() as session:
            totals = await self._claim_totals(session, guild_id, start, as_of)
        rows = [
            BoardRow(discord_user_id=uid, label="", value=float(points), verified=True)
            for uid, points in totals.items()
            if points > 0
        ]
        rows.sort(key=lambda r: r.value, reverse=True)
        return Board(rows=rows, period=period, stale_platforms=[])

    async def composite_board(
        self, guild_id: int, period: Period, as_of: datetime | None = None
    ) -> Board:
        as_of = as_of or utcnow()
        start = period_start(period, as_of)
        platform_values: dict[str, dict[int, float]] = {}
        verified_by_user: dict[int, bool] = {}
        async with self._db.session() as session:
            for platform in self._adapters.platforms:
                links = await self._guild_links(session, guild_id, platform)
                values: dict[int, float] = {}
                for link in links:
                    values[link.discord_user_id] = float(
                        await self._link_delta(session, link.id, start, as_of)
                    )
                    verified_by_user[link.discord_user_id] = (
                        verified_by_user.get(link.discord_user_id, True) and link.verified
                    )
                platform_values[platform.value] = values
            claim_totals = await self._claim_totals(session, guild_id, start, as_of)
        if claim_totals:
            platform_values[CLAIMS_KEY] = {u: float(p) for u, p in claim_totals.items()}
        scores = composite_scores(platform_values, self._scoring.weights)
        rows = [
            BoardRow(
                discord_user_id=uid,
                label="",
                value=score,
                verified=verified_by_user.get(uid, True),
            )
            for uid, score in scores.items()
            if score > 0
        ]
        rows.sort(key=lambda r: r.value, reverse=True)
        stale = [p.value for p in self._adapters.platforms if self._health.is_stale(p)]
        return Board(rows=rows, period=period, stale_platforms=stale)

    async def _guild_links(
        self, session: AsyncSession, guild_id: int, platform: Platform
    ) -> list[AccountLink]:
        guild = await session.get(Guild, guild_id)
        stmt = (
            select(AccountLink)
            .join(
                GuildMember,
                GuildMember.discord_user_id == AccountLink.discord_user_id,
            )
            .where(
                GuildMember.guild_id == guild_id,
                GuildMember.hidden.is_(False),
                AccountLink.platform == platform.value,
            )
        )
        if guild is not None and guild.require_verified:
            stmt = stmt.where(AccountLink.verified.is_(True))
        return list(await session.scalars(stmt))

    async def _link_delta(
        self,
        session: AsyncSession,
        link_id: int,
        start: datetime | None,
        until: datetime,
    ) -> int:
        """Fetch the compact history points_delta() needs: the last snapshot
        at/before the period start plus everything in (start, until]."""
        if start is None:
            latest = await session.execute(
                select(Snapshot.taken_at, Snapshot.points)
                .where(Snapshot.link_id == link_id, Snapshot.taken_at <= until)
                .order_by(Snapshot.taken_at.desc())
                .limit(1)
            )
            row = latest.first()
            return points_delta([tuple(row)] if row else [], None)
        baseline = (
            await session.execute(
                select(Snapshot.taken_at, Snapshot.points)
                .where(Snapshot.link_id == link_id, Snapshot.taken_at <= start)
                .order_by(Snapshot.taken_at.desc())
                .limit(1)
            )
        ).first()
        in_period = (
            await session.execute(
                select(Snapshot.taken_at, Snapshot.points)
                .where(
                    Snapshot.link_id == link_id,
                    Snapshot.taken_at > start,
                    Snapshot.taken_at <= until,
                )
                .order_by(Snapshot.taken_at.asc())
            )
        ).all()
        history = ([tuple(baseline)] if baseline else []) + [tuple(r) for r in in_period]
        return points_delta(history, start)

    async def _claim_totals(
        self,
        session: AsyncSession,
        guild_id: int,
        start: datetime | None,
        until: datetime,
    ) -> dict[int, int]:
        stmt = (
            select(Claim.discord_user_id, func.sum(Claim.points))
            .join(
                GuildMember,
                (GuildMember.discord_user_id == Claim.discord_user_id)
                & (GuildMember.guild_id == Claim.guild_id),
            )
            .where(
                Claim.guild_id == guild_id,
                Claim.status == "approved",
                Claim.reviewed_at <= until,
                GuildMember.hidden.is_(False),
            )
            .group_by(Claim.discord_user_id)
        )
        if start is not None:
            stmt = stmt.where(Claim.reviewed_at >= start)
        return {uid: int(total or 0) for uid, total in await session.execute(stmt)}
