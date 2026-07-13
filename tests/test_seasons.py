from __future__ import annotations

import pytest
from aioresponses import aioresponses

from hackqueue.adapters.htb import (
    URL_SEASON_LIST,
    URL_SEASON_MACHINE_ACTIVE,
    URL_SEASON_MACHINES,
    HTBAdapter,
)
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.db.models import AccountLink, Guild, GuildMember, Solve
from hackqueue.services.seasons import SeasonService

# Captured from the live API on 2026-07-13.
SEASON_LIST = {
    "data": [
        {"id": 14, "name": "Season 10", "subtitle": "old", "state": "ended", "active": False},
        {
            "id": 15,
            "name": "Season 11",
            "subtitle": "Season of the Punk",
            "state": "active",
            "active": True,
            "weeks": 13,
            "current_week": 8,
            "players": 11351,
            "end_date": "2026-08-22T19:00:00.000000Z",
        },
    ]
}
MACHINE_ACTIVE = {
    "data": {
        "id": 921,
        "name": "Paperwork",
        "os": "Linux",
        "difficulty_text": "Easy",
        "is_released": True,
        "release_time": "2026-07-11T19:00:00.000Z",
    }
}
# Note: unreleased future weeks omit both is_released and active.
SEASON_MACHINES = {
    "data": [
        {
            "id": 918,
            "name": "Enigma",
            "os": "Linux",
            "difficulty_text": "Easy",
            "is_released": True,
        },
        {
            "id": 921,
            "name": "Paperwork",
            "os": "Linux",
            "difficulty_text": "Easy",
            "is_released": True,
            "active": True,
        },
        {"id": 930, "name": "FutureBox", "os": "Windows", "difficulty_text": "Hard"},  # unreleased
    ]
}


@pytest.fixture
def htb(http):
    return HTBAdapter(http, "token")


@pytest.fixture
def seasons(db, htb):
    reg = AdapterRegistry()
    reg.register(htb)
    return SeasonService(db, reg)


def _mock_season(m):
    m.get(URL_SEASON_LIST, payload=SEASON_LIST)
    m.get(URL_SEASON_MACHINES.format(season_id=15), payload=SEASON_MACHINES)
    m.get(URL_SEASON_MACHINE_ACTIVE, payload=MACHINE_ACTIVE)


async def test_current_season_resolves_active_one(seasons):
    with aioresponses() as m:
        _mock_season(m)
        season = await seasons.current()
    assert season.season_id == 15
    assert season.name == "Season 11"
    assert season.current_week == 8 and season.total_weeks == 13
    assert season.live_machine.name == "Paperwork"
    # unreleased future weeks are parsed but not counted as released
    assert {x.name for x in season.released_machines} == {"Enigma", "Paperwork"}
    assert len(season.machines) == 3


async def test_no_active_season_returns_none(seasons):
    ended = {"data": [{"id": 14, "name": "Season 10", "state": "ended", "active": False}]}
    with aioresponses() as m:
        m.get(URL_SEASON_LIST, payload=ended)
        assert await seasons.current() is None


async def test_standings_count_rooted_season_boxes(db, seasons):
    """The per-server race: how many of this season's released boxes each
    member has ROOTED — built from our own solve data, not HTB's per-user
    season endpoint (which the single bot token can't read for others)."""
    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=1))
        session.add(GuildMember(guild_id=1, discord_user_id=10))
        session.add(GuildMember(guild_id=1, discord_user_id=20))
        a = AccountLink(
            discord_user_id=10, platform="htb", platform_user_id="1", platform_username="ace"
        )
        b = AccountLink(
            discord_user_id=20, platform="htb", platform_user_id="2", platform_username="newb"
        )
        session.add_all([a, b])
        await session.flush()
        # ace rooted both released season boxes; also a non-season box (ignored)
        session.add_all(
            [
                Solve(
                    link_id=a.id, platform="htb", item_ref="918", item_name="Enigma", kind="root"
                ),
                Solve(
                    link_id=a.id, platform="htb", item_ref="921", item_name="Paperwork", kind="root"
                ),
                Solve(
                    link_id=a.id, platform="htb", item_ref="500", item_name="OldBox", kind="root"
                ),
                # a USER own on a season box shouldn't count as rooted
                Solve(
                    link_id=b.id, platform="htb", item_ref="918", item_name="Enigma", kind="user"
                ),
            ]
        )
    with aioresponses() as m:
        _mock_season(m)
        season = await seasons.current()
    standings = await seasons.standings(1, season)
    by_user = {s.discord_user_id: s for s in standings}
    assert by_user[10].owned == 2 and by_user[10].total == 2  # ace leads
    assert by_user[20].owned == 0  # user-own doesn't count as rooted
    assert standings[0].discord_user_id == 10
