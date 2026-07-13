"""Per-member detail: everything behind clicking a name on the board.

All of it comes from data the poller already stores — snapshots (a score
series), solves (what they actually did), claims — so nothing here costs a
platform API call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from hackqueue.adapters.base import Platform
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.db.models import AccountLink, CatalogBox, Claim, Snapshot, Solve, utcnow
from hackqueue.db.session import Database
from hackqueue.services.scoring import Period, period_start, points_delta

#: How much score history the sparkline shows.
SERIES_DAYS = 60
#: Weeks in the activity strip.
ACTIVITY_WEEKS = 12
RECENT_SOLVES = 12


@dataclass
class PlatformDetail:
    platform: str
    username: str
    profile_url: str | None
    verified: bool
    verifiable: bool
    status: str
    score: int | None
    rank: int | None
    weekly_gain: int
    monthly_gain: int
    #: (iso timestamp, score) — drives the sparkline
    series: list[tuple[str, int]] = field(default_factory=list)
    #: platform-specific extras (HTB: machine owns, Pro Lab flags, bloods…)
    counters: dict[str, int] = field(default_factory=dict)


@dataclass
class SolveDetail:
    platform: str
    name: str
    kind: str
    solved_at: str | None
    first_blood: bool
    url: str | None


@dataclass
class MemberDetail:
    discord_user_id: int
    platforms: list[PlatformDetail]
    recent_solves: list[SolveDetail]
    #: solves per ISO week for the last ACTIVITY_WEEKS, oldest first
    activity: list[dict[str, Any]]
    claims_approved: int
    claims_points: int
    solve_streak_weeks: int
    total_solves: int


PROFILE_URLS = {
    Platform.HTB.value: "https://app.hackthebox.com/profile/{id}",
    Platform.THM.value: "https://tryhackme.com/p/{id}",
    Platform.ROOTME.value: "https://www.root-me.org/?page=info_membre&id_auteur={id}",
}


class ProfileService:
    def __init__(self, db: Database, adapters: AdapterRegistry) -> None:
        self._db = db
        self._adapters = adapters

    def _verifiable(self, platform: str) -> bool:
        try:
            adapter = self._adapters.get(Platform(platform))
        except ValueError:
            return False
        return bool(adapter and getattr(adapter, "supports_verification", False))

    async def member(
        self, guild_id: int, discord_user_id: int, as_of: datetime | None = None
    ) -> MemberDetail | None:
        """``as_of`` pins the clock (periods, the score window, the activity
        strip). Live callers leave it None; tests pin it."""
        now = as_of or utcnow()
        async with self._db.session() as session:
            links = list(
                await session.scalars(
                    select(AccountLink).where(AccountLink.discord_user_id == discord_user_id)
                )
            )
            claims = list(
                await session.scalars(
                    select(Claim).where(
                        Claim.guild_id == guild_id,
                        Claim.discord_user_id == discord_user_id,
                        Claim.status == "approved",
                    )
                )
            )
            if not links and not claims:
                return None

            platforms = [await self._platform_detail(session, link, now) for link in links]

            link_ids = [link.id for link in links]
            solves: list[Solve] = []
            total_solves = 0
            if link_ids:
                solves = list(
                    await session.scalars(
                        select(Solve)
                        .where(Solve.link_id.in_(link_ids))
                        .order_by(Solve.solved_at.desc().nullslast(), Solve.id.desc())
                        .limit(RECENT_SOLVES)
                    )
                )
                total_solves = int(
                    await session.scalar(
                        select(func.count()).select_from(Solve).where(Solve.link_id.in_(link_ids))
                    )
                    or 0
                )
            recent = [await self._solve_detail(session, s) for s in solves]
            activity = await self._activity(session, link_ids, now)

        return MemberDetail(
            discord_user_id=discord_user_id,
            platforms=platforms,
            recent_solves=recent,
            activity=activity,
            claims_approved=len(claims),
            claims_points=sum(c.points for c in claims),
            solve_streak_weeks=_streak(activity),
            total_solves=total_solves,
        )

    async def _platform_detail(self, session, link: AccountLink, now: datetime) -> PlatformDetail:
        rows = list(
            await session.execute(
                select(Snapshot.taken_at, Snapshot.points, Snapshot.rank, Snapshot.counters)
                .where(
                    Snapshot.link_id == link.id,
                    Snapshot.taken_at >= now - timedelta(days=SERIES_DAYS),
                )
                .order_by(Snapshot.taken_at.asc())
            )
        )
        history = [(taken_at, points) for taken_at, points, _, _ in rows]
        latest = rows[-1] if rows else None
        url_template = PROFILE_URLS.get(link.platform)
        return PlatformDetail(
            platform=link.platform,
            username=link.platform_username,
            profile_url=url_template.format(id=link.platform_user_id) if url_template else None,
            verified=link.verified,
            verifiable=self._verifiable(link.platform),
            status=link.status,
            score=int(latest[1]) if latest else None,
            rank=int(latest[2]) if latest and latest[2] is not None else None,
            weekly_gain=points_delta(history, period_start(Period.WEEKLY, now)),
            monthly_gain=points_delta(history, period_start(Period.MONTHLY, now)),
            series=[(t.isoformat(), int(p)) for t, p in history],
            counters={k: int(v) for k, v in (latest[3] or {}).items()} if latest else {},
        )

    async def _solve_detail(self, session, solve: Solve) -> SolveDetail:
        url = None
        if solve.platform == Platform.HTB.value and solve.kind in ("user", "root"):
            box = await session.scalar(
                select(CatalogBox).where(
                    CatalogBox.platform == solve.platform,
                    CatalogBox.platform_ref == solve.item_ref,
                )
            )
            url = box.url if box else None
        when = solve.solved_at or solve.first_seen_at
        return SolveDetail(
            platform=solve.platform,
            name=solve.item_name,
            kind=solve.kind,
            solved_at=when.isoformat() if when else None,
            first_blood=solve.first_blood,
            url=url,
        )

    async def _activity(self, session, link_ids: list[int], now: datetime) -> list[dict[str, Any]]:
        """Solves per week for the last ACTIVITY_WEEKS. Backfilled solves (a
        member's pre-link history) are excluded — they'd paint a wall of fake
        activity in the week someone joined."""
        weeks: list[dict[str, Any]] = []
        this_week = period_start(Period.WEEKLY, now)
        assert this_week is not None
        if not link_ids:
            return [
                {"week": (this_week - timedelta(weeks=n)).date().isoformat(), "solves": 0}
                for n in reversed(range(ACTIVITY_WEEKS))
            ]
        start = this_week - timedelta(weeks=ACTIVITY_WEEKS - 1)
        rows = list(
            await session.execute(
                select(Solve.solved_at, Solve.first_seen_at).where(
                    Solve.link_id.in_(link_ids),
                    Solve.backfilled.is_(False),
                )
            )
        )
        counts: dict[str, int] = {}
        for solved_at, first_seen_at in rows:
            when = solved_at or first_seen_at
            if when is None or when < start:
                continue
            bucket = period_start(Period.WEEKLY, when)
            if bucket is not None:
                counts[bucket.date().isoformat()] = counts.get(bucket.date().isoformat(), 0) + 1
        for n in reversed(range(ACTIVITY_WEEKS)):
            key = (this_week - timedelta(weeks=n)).date().isoformat()
            weeks.append({"week": key, "solves": counts.get(key, 0)})
        return weeks


def _streak(activity: list[dict[str, Any]]) -> int:
    """Consecutive weeks with at least one solve, counting back from the most
    recent week. The current week doesn't break a streak if it's still empty —
    it hasn't finished yet."""
    weeks = [w["solves"] for w in activity]
    if not weeks:
        return 0
    if weeks[-1] == 0:  # current week not started; judge from last week
        weeks = weeks[:-1]
    streak = 0
    for count in reversed(weeks):
        if count == 0:
            break
        streak += 1
    return streak
