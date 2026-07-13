from __future__ import annotations

import pytest
from aioresponses import aioresponses

from hackqueue.adapters.base import (
    AuthExpired,
    Platform,
    PlatformUser,
    ProfileNotFound,
    ProfilePrivate,
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

# Shapes below mirror real responses captured from the live API (2026-07-12).
PROFILE_PAYLOAD = {
    "profile": {
        "id": 1337,
        "name": "0xdf",
        "points": 420,
        "rank": "Guru",
        "ranking": 12,
        "user_owns": 150,
        "system_owns": 145,
        "user_bloods": 3,
        "system_bloods": 2,
        "respects": 999,
        "public": True,
        "twitter": None,
        "github": None,
        "linkedin": None,
        "cv": None,
    }
}


@pytest.fixture
def adapter(http):
    return HTBAdapter(http, app_token="test-token")


async def test_get_profile(adapter):
    with aioresponses() as m:
        _mock_all(m)
        stats = await adapter.get_profile(USER)
    assert stats.rank == 12
    assert stats.username == "0xdf"
    assert stats.counters["user_owns"] == 150
    assert stats.counters["system_owns"] == 145
    assert stats.counters["user_bloods"] == 3
    assert stats.counters["system_bloods"] == 2
    assert stats.counters["respects"] == 999


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


@pytest.mark.parametrize("status", [400, 404])
async def test_bad_user_id_raises_profile_not_found(adapter, status):
    """Live API answers 400 for an invalid id (not 404 as docs claimed)."""
    with aioresponses() as m:
        m.get(
            URL_PROFILE_BASIC.format(user_id="1337"),
            status=status,
            payload={"message": {"user_id": ["The selected user id is invalid."]}},
        )
        with pytest.raises(ProfileNotFound):
            await adapter.get_profile(USER)


async def test_private_profile_is_200_with_public_false(adapter):
    """A private profile is NOT an error status — it's public=false."""
    payload = {"profile": {**PROFILE_PAYLOAD["profile"], "public": False}}
    with aioresponses() as m:
        m.get(URL_PROFILE_BASIC.format(user_id="1337"), payload=payload)
        with pytest.raises(ProfilePrivate, match="Public Profile"):
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


ACTIVITY_PAGE_1 = {
    "data": [
        {
            "blood": True,
            "type": "root",
            "id": 948,
            "name": "Nexus",
            "points": 0,
            "ownDate": "2026-07-10T08:26:37.000Z",
        },
        {
            "blood": False,
            "type": "user",
            "id": 948,
            "name": "Nexus",
            "points": 0,
            "ownDate": "2026-07-09T10:00:00.000Z",
        },
        {
            "blood": False,
            "type": "challenge",
            "categoryName": "Web",
            "id": 117,
            "name": "wafwaf",
            "points": 10,
            "ownDate": "2026-07-08T10:00:00.000Z",
        },
        {"type": "fortress", "id": 1, "name": "Jet", "points": 20},
    ],
    "meta": {"page": 1, "lastPage": 2, "totalItems": 5},
}
ACTIVITY_PAGE_2 = {
    "data": [
        {
            "blood": False,
            "type": "user",
            "id": 12,
            "name": "Lame",
            "points": 0,
            "ownDate": "2025-01-02T10:00:00.000Z",
        }
    ],
    "meta": {"page": 2, "lastPage": 2, "totalItems": 5},
}


async def test_activity_maps_v5_shape_to_solves(adapter):
    """Live v5 fields are type/ownDate/blood — NOT object_type/date/first_blood
    as the community docs claim; parsing the old names yields zero solves."""
    with aioresponses() as m:
        m.get(URL_PROFILE_ACTIVITY.format(user_id="1337", page=1), payload=ACTIVITY_PAGE_1)
        solves = await adapter.get_recent_solves(USER)
    assert [s.kind for s in solves] == ["root", "user", "challenge"]  # fortress skipped
    assert solves[0].first_blood is True
    assert solves[0].item_ref == "948"
    assert solves[0].name == "Nexus"
    assert solves[0].solved_at is not None and solves[0].solved_at.year == 2026


async def test_shallow_poll_reads_only_page_one(adapter):
    """Only one mock registered: a second page fetch would error."""
    with aioresponses() as m:
        m.get(URL_PROFILE_ACTIVITY.format(user_id="1337", page=1), payload=ACTIVITY_PAGE_1)
        solves = await adapter.get_recent_solves(USER, deep=False)
    assert len(solves) == 3


async def test_deep_poll_walks_all_pages(adapter):
    with aioresponses() as m:
        m.get(URL_PROFILE_ACTIVITY.format(user_id="1337", page=1), payload=ACTIVITY_PAGE_1)
        m.get(URL_PROFILE_ACTIVITY.format(user_id="1337", page=2), payload=ACTIVITY_PAGE_2)
        solves = await adapter.get_recent_solves(USER, deep=True)
    assert [s.name for s in solves] == ["Nexus", "Nexus", "wafwaf", "Lame"]


async def test_verification_reads_social_fields(adapter):
    """HTB has no bio field — the token lives in a social-link field."""
    payload = {"profile": {**PROFILE_PAYLOAD["profile"], "twitter": "https://x.com/hq-ab12cd34"}}
    with aioresponses() as m:
        m.get(URL_PROFILE_BASIC.format(user_id="1337"), payload=payload)
        haystack = await adapter.get_verification_token_haystack(USER)
    assert "hq-ab12cd34" in haystack


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
            "star": 4,  # the LIST endpoint sends numeric "star" (live-verified)
            "labels": [{"color": "blue", "name": "SEASONAL"}],
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
    assert machines[0]["stars"] == pytest.approx(4.0)
    assert machines[0]["tags"] == ["SEASONAL"]
    assert machines[2]["platform_ref"] == "3"
    assert machines[0]["difficulty"] == "easy"


def test_normalize_machine_accepts_both_rating_keys():
    """List endpoint sends numeric `star`; detail sends `stars` (often a string)."""
    from hackqueue.adapters.htb import HTBAdapter as A

    assert A._normalize_machine({"id": 1, "star": 4}, retired=False)["stars"] == 4.0
    assert A._normalize_machine({"id": 1, "stars": "4.6"}, retired=True)["stars"] == 4.6
    assert A._normalize_machine({"id": 1}, retired=False)["stars"] is None


PROLAB_PAYLOAD = {
    "profile": {
        "prolabs": [
            {"name": "Dante", "owned_flags": 27, "total_flags": 27, "completion_percentage": 100},
            {"name": "Zephyr", "owned_flags": 5, "total_flags": 17, "completion_percentage": 29},
            {"name": "RastaLabs", "owned_flags": 0, "total_flags": 20, "completion_percentage": 0},
        ]
    }
}
FORTRESS_PAYLOAD = {
    "profile": {"fortresses": [{"name": "Jet", "owned_flags": 3, "total_flags": 11}]}
}
CHALLENGE_PAYLOAD = {"profile": {"challenge_owns": {"solved": 6, "total": 838}}}


def _mock_all(m, *, prolab=PROLAB_PAYLOAD, fortress=FORTRESS_PAYLOAD, challenges=CHALLENGE_PAYLOAD):
    from hackqueue.adapters.htb import (
        URL_PROGRESS_CHALLENGES,
        URL_PROGRESS_FORTRESS,
        URL_PROGRESS_PROLAB,
    )

    m.get(URL_PROFILE_BASIC.format(user_id="1337"), payload=PROFILE_PAYLOAD)
    m.get(URL_PROGRESS_CHALLENGES.format(user_id="1337"), payload=challenges)
    m.get(URL_PROGRESS_PROLAB.format(user_id="1337"), payload=prolab)
    m.get(URL_PROGRESS_FORTRESS.format(user_id="1337"), payload=fortress)


async def test_score_counts_flags_not_htb_rank_points(adapter):
    """HTB zeroes a machine's points when it retires, so rank points are a dead
    metric (0 for someone with 8 owns and a finished Pro Lab). Score flags."""
    with aioresponses() as m:
        _mock_all(m)
        stats = await adapter.get_profile(USER)
    # 150 user + 145 system owns, 6 challenges, 27+5 Pro Lab, 3 Fortress
    assert stats.points == 150 + 145 + 6 + 32 + 3
    assert stats.counters["prolab_flags"] == 32
    assert stats.counters["prolabs_completed"] == 1  # Dante at 100%
    assert stats.counters["fortress_flags"] == 3
    assert stats.counters["challenges"] == 6
    assert stats.counters["htb_rank_points"] == 420  # kept for reference only


async def test_missing_progress_sections_cost_only_their_flags(adapter):
    """A 404 on Pro Labs must not fail the whole poll — the member still gets
    a snapshot with their machine owns."""
    from hackqueue.adapters.htb import (
        URL_PROGRESS_CHALLENGES,
        URL_PROGRESS_FORTRESS,
        URL_PROGRESS_PROLAB,
    )

    with aioresponses() as m:
        m.get(URL_PROFILE_BASIC.format(user_id="1337"), payload=PROFILE_PAYLOAD)
        m.get(URL_PROGRESS_CHALLENGES.format(user_id="1337"), status=404, payload={})
        m.get(URL_PROGRESS_PROLAB.format(user_id="1337"), status=404, payload={})
        m.get(URL_PROGRESS_FORTRESS.format(user_id="1337"), status=404, payload={})
        stats = await adapter.get_profile(USER)
    assert stats.points == 150 + 145  # machine owns still counted
    assert stats.counters["prolab_flags"] == 0
