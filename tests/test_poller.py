from __future__ import annotations

import pytest
from sqlalchemy import select

from hackqueue.adapters.base import Platform, ProfileStats, SolveEvent
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.config import Settings
from hackqueue.db.models import AccountLink, Solve
from hackqueue.services.health import HealthRegistry
from hackqueue.services.snapshots import PollerService


def stats(points: int) -> ProfileStats:
    return ProfileStats(platform=Platform.HTB, user_id="1", username="x", points=points, rank=None)


def solve(ref: str) -> SolveEvent:
    return SolveEvent(
        platform=Platform.HTB, item_ref=ref, name=ref, kind="root", points=10, solved_at=None
    )


@pytest.fixture
async def poller_and_link(db):
    settings = Settings(discord_token="x", _env_file=None)
    poller = PollerService(db, AdapterRegistry(), settings, HealthRegistry())
    async with db.session() as session, session.begin():
        link = AccountLink(
            discord_user_id=1, platform="htb", platform_user_id="1", platform_username="x"
        )
        session.add(link)
        await session.flush()
        session.expunge(link)
    return poller, link


async def test_first_poll_marks_solves_backfilled(db, poller_and_link):
    """A member's pre-link history imported on their first poll must not look
    like fresh activity (it would flood the weekly recap)."""
    poller, link = poller_and_link
    await poller._store(link, stats(100), [solve("1"), solve("2")])
    await poller._store(link, stats(120), [solve("1"), solve("2"), solve("3")])
    async with db.session() as session:
        rows = {
            ref: backfilled
            for ref, backfilled in await session.execute(select(Solve.item_ref, Solve.backfilled))
        }
    assert rows == {"1": True, "2": True, "3": False}


async def test_store_dedupes_solves_across_polls(db, poller_and_link):
    poller, link = poller_and_link
    await poller._store(link, stats(100), [solve("1")])
    await poller._store(link, stats(100), [solve("1"), solve("1")])
    async with db.session() as session:
        count = len(list(await session.scalars(select(Solve.id))))
    assert count == 1


async def test_poll_one_stores_a_snapshot_immediately(db):
    """Linking should surface stats without waiting for the scheduled cycle."""
    from sqlalchemy import select

    from hackqueue.adapters.base import Platform, ProfileStats
    from hackqueue.adapters.registry import AdapterRegistry
    from hackqueue.config import Settings
    from hackqueue.db.models import AccountLink, Snapshot
    from hackqueue.services.health import HealthRegistry

    class _Stub:
        platform = Platform.HTB
        supports_verification = True

        async def poll(self, user, *, deep=False):
            return ProfileStats(
                platform=Platform.HTB,
                user_id="1",
                username="x",
                points=41,
                rank=1000,
                counters={"prolab_flags": 33},
            ), []

    reg = AdapterRegistry()
    reg.register(_Stub())
    poller = PollerService(db, reg, Settings(discord_token="x", _env_file=None), HealthRegistry())
    async with db.session() as s, s.begin():
        link = AccountLink(
            discord_user_id=1, platform="htb", platform_user_id="1", platform_username="x"
        )
        s.add(link)
        await s.flush()
        link_id = link.id
    assert await poller.poll_one(link_id) is True
    async with db.session() as s:
        snap = await s.scalar(select(Snapshot).where(Snapshot.link_id == link_id))
    assert snap.points == 41


async def test_poll_one_swallows_failures(db):
    """A failed immediate poll must never bubble up into /link."""
    from hackqueue.adapters.base import Platform, PlatformUnavailable
    from hackqueue.adapters.registry import AdapterRegistry
    from hackqueue.config import Settings
    from hackqueue.db.models import AccountLink
    from hackqueue.services.health import HealthRegistry

    class _Down:
        platform = Platform.HTB
        supports_verification = False

        async def poll(self, user, *, deep=False):
            raise PlatformUnavailable("down")

    reg = AdapterRegistry()
    reg.register(_Down())
    poller = PollerService(db, reg, Settings(discord_token="x", _env_file=None), HealthRegistry())
    async with db.session() as s, s.begin():
        link = AccountLink(
            discord_user_id=1, platform="htb", platform_user_id="1", platform_username="x"
        )
        s.add(link)
        await s.flush()
        link_id = link.id
    assert await poller.poll_one(link_id) is False  # no raise
