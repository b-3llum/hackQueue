from __future__ import annotations

import pytest
from aioresponses import aioresponses

from hackqueue.adapters.base import Platform, PlatformUnavailable, PlatformUser, ProfileNotFound
from hackqueue.adapters.thm import (
    URL_COMPLETED_COUNT,
    URL_COMPLETED_ROOMS,
    URL_DISCORD_USER,
    URL_PUBLIC_PROFILE,
    THMAdapter,
)

USER = PlatformUser(platform=Platform.THM, user_id="neo", username="neo")
CHALLENGE_KW = {
    "status": 429,
    "headers": {"x-vercel-mitigated": "challenge", "Content-Type": "text/html"},
    "body": "<html>Vercel Security Checkpoint</html>",
}


@pytest.fixture
def adapter(http):
    return THMAdapter(http)


async def test_get_profile_happy_path(adapter):
    with aioresponses() as m:
        m.get(
            URL_DISCORD_USER.format(username="neo"),
            payload={"userRank": 1234, "points": 9000, "avatar": "x", "subscribed": 1},
        )
        m.get(URL_COMPLETED_COUNT.format(username="neo"), payload=42)
        stats = await adapter.get_profile(USER)
    assert stats.points == 9000
    assert stats.rank == 1234
    assert stats.counters["rooms_completed"] == 42
    assert stats.counters["subscribed"] == 1


async def test_challenge_raises_platform_unavailable(adapter):
    with aioresponses() as m:
        m.get(URL_DISCORD_USER.format(username="neo"), **CHALLENGE_KW)
        with pytest.raises(PlatformUnavailable, match="challenge"):
            await adapter.get_profile(USER)


async def test_profile_still_works_when_rooms_count_is_challenged(adapter):
    # Partial degradation: the primary endpoint answers, the secondary is
    # challenged — the poll must still produce a snapshot.
    with aioresponses() as m:
        m.get(
            URL_DISCORD_USER.format(username="neo"),
            payload={"userRank": 5, "points": 100},
        )
        m.get(URL_COMPLETED_COUNT.format(username="neo"), **CHALLENGE_KW)
        stats = await adapter.get_profile(USER)
    assert stats.points == 100
    assert "rooms_completed" not in stats.counters


async def test_unexpected_shape_is_not_trusted(adapter):
    with aioresponses() as m:
        m.get(URL_DISCORD_USER.format(username="neo"), payload={"weird": "shape"})
        with pytest.raises(ProfileNotFound):
            await adapter.get_profile(USER)


async def test_resolve_user_captures_v2_ids_best_effort(adapter):
    with aioresponses() as m:
        m.get(URL_DISCORD_USER.format(username="neo"), payload={"userRank": 1, "points": 10})
        m.get(
            URL_PUBLIC_PROFILE.format(username="neo"),
            payload={"data": {"userPublicId": 424242, "userId": "65f0c0ffee0dd00dcafebabe"}},
        )
        user = await adapter.resolve_user("@neo")
    assert user.user_id == "neo"
    assert user.extra_ids["user_public_id"] == "424242"
    assert user.extra_ids["user_hash"] == "65f0c0ffee0dd00dcafebabe"


async def test_resolve_user_survives_v2_challenge(adapter):
    with aioresponses() as m:
        m.get(URL_DISCORD_USER.format(username="neo"), payload={"userRank": 1, "points": 10})
        m.get(URL_PUBLIC_PROFILE.format(username="neo"), **CHALLENGE_KW)
        user = await adapter.resolve_user("neo")
    assert user.extra_ids == {}


async def test_solves_without_user_hash_returns_empty(adapter):
    assert await adapter.get_recent_solves(USER) == []


async def test_solves_with_user_hash(adapter):
    user = PlatformUser(
        platform=Platform.THM,
        user_id="neo",
        username="neo",
        extra_ids={"user_hash": "65f0c0ffee0dd00dcafebabe"},
    )
    with aioresponses() as m:
        m.get(
            URL_COMPLETED_ROOMS.format(user_hash="65f0c0ffee0dd00dcafebabe"),
            payload={"data": [{"code": "vulnversity", "title": "Vulnversity"}]},
        )
        solves = await adapter.get_recent_solves(user)
    assert len(solves) == 1
    assert solves[0].item_ref == "vulnversity"
    assert solves[0].kind == "room"


async def test_solves_challenge_degrades_to_empty(adapter):
    user = PlatformUser(
        platform=Platform.THM, user_id="neo", username="neo", extra_ids={"user_hash": "abc"}
    )
    with aioresponses() as m:
        m.get(URL_COMPLETED_ROOMS.format(user_hash="abc"), **CHALLENGE_KW)
        assert await adapter.get_recent_solves(user) == []
