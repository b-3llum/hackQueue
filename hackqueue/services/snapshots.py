"""Background poller: periodically snapshots every linked profile.

Isolation guarantees (the "one platform outage never breaks the rest" rule):
- each platform runs in its own task with its own interval (±10% jitter);
- an auth/outage error aborts only that platform's current cycle and marks it
  degraded in the health registry — boards keep rendering from the last
  snapshots with a staleness marker;
- per-profile errors (private/deleted) only update that link's status.
"""

from __future__ import annotations

import asyncio
import random

import aiohttp
from sqlalchemy import select

from hackqueue.adapters.base import (
    AuthExpired,
    PlatformAdapter,
    PlatformUnavailable,
    ProfileNotFound,
    ProfilePrivate,
    ProfileStats,
    RateLimited,
    SolveEvent,
)
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.config import Settings
from hackqueue.db.models import AccountLink, Snapshot, Solve
from hackqueue.db.session import Database
from hackqueue.log import get_logger
from hackqueue.services.health import HealthRegistry
from hackqueue.services.linking import link_to_platform_user

log = get_logger(__name__)

#: Gentle spacing between member polls, on top of the per-host HTTP limiter.
PER_LINK_SPACING_SECONDS = 2.0


class PollerService:
    def __init__(
        self,
        db: Database,
        adapters: AdapterRegistry,
        settings: Settings,
        health: HealthRegistry,
    ) -> None:
        self._db = db
        self._adapters = adapters
        self._health = health
        self._intervals = {
            "htb": settings.poll_interval_htb,
            "thm": settings.poll_interval_thm,
            "rootme": settings.poll_interval_rootme,
        }
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        for adapter in self._adapters:
            minutes = self._intervals.get(adapter.platform.value, 60)
            self._tasks.append(
                asyncio.create_task(
                    self._platform_loop(adapter, minutes),
                    name=f"poller:{adapter.platform.value}",
                )
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _platform_loop(self, adapter: PlatformAdapter, interval_minutes: int) -> None:
        await asyncio.sleep(random.uniform(10, 60))  # stagger platform loops at boot
        while True:
            try:
                await self.poll_platform(adapter)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll_cycle_crashed", platform=adapter.platform.value)
            await asyncio.sleep(interval_minutes * 60 * random.uniform(0.9, 1.1))

    async def poll_platform(self, adapter: PlatformAdapter) -> None:
        platform = adapter.platform
        async with self._db.session() as session:
            links = list(
                await session.scalars(
                    select(AccountLink).where(AccountLink.platform == platform.value)
                )
            )
        if not links:
            return
        log.debug("poll_cycle_start", platform=platform.value, links=len(links))
        for link in links:
            try:
                # First poll of a link walks the full solve history (backfill);
                # later polls take the adapter's cheap recent-only path.
                deep = not await self._has_snapshot(link.id)
                stats, solves = await adapter.poll(link_to_platform_user(link), deep=deep)
            except AuthExpired as exc:
                # Bot-level credential problem: every remaining link would fail too.
                self._health.record_error(platform, exc)
                return
            except (RateLimited, PlatformUnavailable) as exc:
                self._health.record_error(platform, exc)
                return
            except (aiohttp.ClientError, TimeoutError) as exc:
                # Transport failure (DNS, TLS, timeout) — same treatment as an
                # outage so /health and staleness markers reflect reality.
                self._health.record_error(
                    platform, PlatformUnavailable(f"network error: {exc or type(exc).__name__}")
                )
                return
            except ProfileNotFound as exc:
                await self._set_link_status(
                    link.id, "private" if isinstance(exc, ProfilePrivate) else "not_found"
                )
                continue
            await self._store(link, stats, solves)
            self._health.record_success(platform)
            await asyncio.sleep(PER_LINK_SPACING_SECONDS)

    async def _store(
        self, link: AccountLink, stats: ProfileStats, solves: list[SolveEvent]
    ) -> None:
        async with self._db.session() as session, session.begin():
            db_link = await session.get(AccountLink, link.id)
            if db_link is None:
                return  # unlinked mid-poll; nothing to store (data was purged)
            db_link.status = "ok"
            if stats.username and stats.username != db_link.platform_username:
                db_link.platform_username = stats.username
            # Solves seen on the very first poll are the member's pre-link
            # history, not fresh activity — flag them so recaps skip them.
            is_first_poll = (
                await session.scalar(
                    select(Snapshot.id).where(Snapshot.link_id == db_link.id).limit(1)
                )
            ) is None
            session.add(
                Snapshot(
                    link_id=db_link.id,
                    points=stats.points,
                    rank=stats.rank,
                    counters=stats.counters,
                )
            )
            if solves:
                existing = {
                    (item_ref, kind)
                    for item_ref, kind in await session.execute(
                        select(Solve.item_ref, Solve.kind).where(Solve.link_id == db_link.id)
                    )
                }
                seen: set[tuple[str, str]] = set()
                for event in solves:
                    key = (event.item_ref, event.kind)
                    if key in existing or key in seen:
                        continue
                    seen.add(key)
                    session.add(
                        Solve(
                            link_id=db_link.id,
                            platform=event.platform.value,
                            item_ref=event.item_ref,
                            item_name=event.name,
                            kind=event.kind,
                            points=event.points,
                            solved_at=event.solved_at,
                            first_blood=event.first_blood,
                            backfilled=is_first_poll,
                        )
                    )

    async def _has_snapshot(self, link_id: int) -> bool:
        async with self._db.session() as session:
            return (
                await session.scalar(
                    select(Snapshot.id).where(Snapshot.link_id == link_id).limit(1)
                )
            ) is not None

    async def _set_link_status(self, link_id: int, status: str) -> None:
        async with self._db.session() as session, session.begin():
            link = await session.get(AccountLink, link_id)
            if link is not None and link.status != status:
                link.status = status
                log.info("link_status_changed", link_id=link_id, status=status)
