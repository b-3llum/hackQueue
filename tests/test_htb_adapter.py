from __future__ import annotations

import pytest
from aioresponses import aioresponses

from hackqueue.adapters.base import (
    AuthExpired,
    Platform,
    PlatformUser,
    ProfileNotFound,
    RateLimited,
)
from hackqueue.adapters.htb import (
    URL_MACHINES_ACTIVE,
    URL_MACHINES_RETIRED,
    URL_PROFILE_ACTIVITY,
    URL_PROFILE_BASIC,
    HTBAdapter,
)

USER = PlatformUser(platform=Platform.HTB, user_id="1337", username="tester")

PROFILE_PAYLOAD = {
    "profile": {
        "id": 1337,
        "name": "0xdf",
        "points": 420,
        "rank": "Guru",
        "ranking": 12,
        "user_owns": 150,
        "system_owns": 145,
        "respects": 999,
    }
}


@pytest.fixture
def adapter(http):
    return HTBAdapter(http, app_token="test-token")


async def test_get_profile(adapter):
    with aioresponses() as m:
        m.get(URL_PROFILE_BASIC.format(user_id="1337"), payload=PROFILE_PAYLOAD)
        stats = await adapter.get_profile(USER)
    assert stats.points == 420
    assert stats.rank == 12
    assert stats.counters == {"user_owns": 150, "system_owns": 145, "respects": 999}
    assert stats.username == "0xdf"


async def test_401_raises_auth_expired(adapter):
    with aioresponses() as m:
        m.get(
            URL_PROFILE_BASIC.format(user_id="1337"),
            status=401,
            payload={"error": "Unauthenticated."},
        )
        with pytest.raises(AuthExpired):
            await adapter.get_profile(USER)


async def test_302_login_redirect_raises_auth_expired(adapter):
    # The live-verified trap: missing Accept header behavior must map to auth error.
    with aioresponses() as m:
        m.get(
            URL_PROFILE_BASIC.format(user_id="1337"),
            status=302,
            headers={"Location": "https://app.hackthebox.com/login"},
        )
        with pytest.raises(AuthExpired):
            await adapter.get_profile(USER)


async def test_404_raises_profile_not_found_with_privacy_hint(adapter):
    with aioresponses() as m:
        m.get(URL_PROFILE_BASIC.format(user_id="1337"), status=404, payload={})
        with pytest.raises(ProfileNotFound, match="public"):
            await adapter.get_profile(USER)


async def test_429_raises_rate_limited(adapter):
    with aioresponses() as m:
        m.get(
            URL_PROFILE_BASIC.format(user_id="1337"),
            status=429,
            payload={},
            repeat=True,  # client retries JSON 429s before giving up
        )
        with pytest.raises(RateLimited):
            await adapter.get_profile(USER)


async def test_activity_maps_to_solves(adapter):
    activity = {
        "profile": {
            "activity": [
                {
                    "date": "2026-07-10T08:26:37.000000Z",
                    "object_type": "machine",
                    "type": "root",
                    "id": 555,
                    "name": "Cascade",
                    "points": 30,
                    "first_blood": True,
                },
                {
                    "date": "2026-07-09T10:00:00.000000Z",
                    "object_type": "machine",
                    "type": "user",
                    "id": 555,
                    "name": "Cascade",
                    "points": 15,
                },
                {
                    "date": "2026-07-08T10:00:00.000000Z",
                    "object_type": "challenge",
                    "type": "challenge",
                    "id": 77,
                    "name": "Crypto Thing",
                    "points": 10,
                },
                {"object_type": "fortress", "id": 1, "name": "Jet", "points": 20},
            ]
        }
    }
    with aioresponses() as m:
        m.get(URL_PROFILE_ACTIVITY.format(user_id="1337"), payload=activity)
        solves = await adapter.get_recent_solves(USER)
    assert [s.kind for s in solves] == ["root", "user", "challenge"]  # fortress skipped
    assert solves[0].first_blood is True
    assert solves[0].item_ref == "555"
    assert solves[0].solved_at is not None and solves[0].solved_at.year == 2026


async def test_resolve_user_accepts_profile_url(adapter):
    with aioresponses() as m:
        m.get(URL_PROFILE_BASIC.format(user_id="1337"), payload=PROFILE_PAYLOAD)
        user = await adapter.resolve_user("https://app.hackthebox.com/profile/1337")
    assert user.user_id == "1337"
    assert user.username == "0xdf"


async def test_resolve_user_rejects_garbage(adapter):
    with pytest.raises(ProfileNotFound):
        await adapter.resolve_user("not-a-number")


async def test_iter_machines_pages_both_endpoints(adapter):
    def machine(mid, name, retired_page):
        return {
            "id": mid,
            "name": name,
            "os": "Linux",
            "difficultyText": "Easy",
            "stars": "4.6",  # string per live API
            "release": "2020-03-14T17:00:00.000000Z",
            "free": False,
        }

    with aioresponses() as m:
        m.get(
            URL_MACHINES_ACTIVE.format(page=1),
            payload={"data": [machine(1, "Active1", False)], "meta": {"last_page": 2}},
        )
        m.get(
            URL_MACHINES_ACTIVE.format(page=2),
            payload={"data": [machine(2, "Active2", False)], "meta": {"last_page": 2}},
        )
        m.get(
            URL_MACHINES_RETIRED.format(page=1),
            payload={"data": [machine(3, "Lame", True)], "meta": {"last_page": 1}},
        )
        machines = [mach async for mach in adapter.iter_machines()]
    assert len(machines) == 3
    assert [m["retired"] for m in machines] == [False, False, True]
    assert machines[0]["stars"] == pytest.approx(4.6)
    assert machines[2]["platform_ref"] == "3"
    assert machines[0]["difficulty"] == "easy"
