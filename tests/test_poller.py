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
