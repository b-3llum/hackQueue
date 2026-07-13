from __future__ import annotations

import pytest
from aioresponses import aioresponses

from hackqueue.adapters.base import Platform, PlatformUnavailable, PlatformUser, ProfileNotFound
from hackqueue.adapters.thm import URL_COMPLETED_ROOMS, URL_PUBLIC_PROFILE, THMAdapter

USER = PlatformUser(platform=Platform.THM, user_id="neo", username="neo")

# Captured from the live API on 2026-07-13.
PROFILE_PAYLOAD = {
    "status": "success",
    "data": {
        "username": "neo",
        "avatar": "https://cdn-images.tryhackme.com/user-avatars/x.png",
        "level": 7,
        "country": "gb",
        "about": "just here to hack",
        "totalPoints": 3834,
        "badgesNumber": 2,
        "completedRoomsNumber": 8,
        "streak": 3,
        "rank": "Top 15%",
        "topPercentage": 15,
        "leagueTier": "bronze",
    },
}
ROOMS_PAGE_1 = {
    "status": "success",
    "data": {
        "docs": [
            {"_id": "1", "code": "mrrobot", "title": "Mr Robot CTF", "difficulty": "medium"},
            {"_id": "2", "code": "vulnversity", "title": "Vulnversity", "difficulty": "easy"},
            {"_id": "3", "title": "No code — skipped"},
        ],
        "totalDocs": 4,
        "page": 1,
        "hasNextPage": True,
    },
}
ROOMS_PAGE_2 = {
    "status": "success",
    "data": {
        "docs": [{"_id": "4", "code": "blue", "title": "Blue"}],
        "page": 2,
        "hasNextPage": False,
    },
}
# A bot-looking UA gets this instead of JSON. THM can turn it back on any time.
CHALLENGE_KW = {
    "status": 429,
    "headers": {"x-vercel-mitigated": "challenge", "Content-Type": "text/html"},
    "body": "<html>Vercel Security Checkpoint</html>",
}


@pytest.fixture
def adapter(http):
    return THMAdapter(http, "hackQueue/test (+https://example.invalid)")


def _profile_url(username: str = "neo") -> str:
    return URL_PUBLIC_PROFILE.format(username=username)


def _rooms_url(page: int, username: str = "neo") -> str:
    return URL_COMPLETED_ROOMS.format(username=username, limit=50, page=page)


def test_user_agent_is_browser_like_but_still_identifiable(adapter):
    """THM 429s bot-looking UAs, so we send a browser UA — but our identifier
    rides along, so we aren't pretending to be a person."""
    ua = adapter._headers["User-Agent"]
    assert ua.startswith("Mozilla/5.0")
    assert "hackQueue" in ua


async def test_get_profile_reads_the_v2_shape(adapter):
    with aioresponses() as m:
        m.get(_profile_url(), payload=PROFILE_PAYLOAD)
        stats = await adapter.get_profile(USER)
    assert stats.points == 3834  # totalPoints
    assert stats.rank is None  # THM publishes no global position any more
    assert stats.counters == {
        "rooms_completed": 8,
        "badges": 2,
        "streak_days": 3,
        "level": 7,
        "top_percent": 15,
    }


async def test_completed_rooms_become_solves(adapter):
    with aioresponses() as m:
        m.get(_rooms_url(1), payload=ROOMS_PAGE_1)
        solves = await adapter.get_recent_solves(USER)
    assert [s.item_ref for s in solves] == ["mrrobot", "vulnversity"]  # code-less doc skipped
    assert solves[0].name == "Mr Robot CTF"
    assert solves[0].kind == "room"


async def test_deep_poll_pages_through_rooms(adapter):
    with aioresponses() as m:
        m.get(_rooms_url(1), payload=ROOMS_PAGE_1)
        m.get(_rooms_url(2), payload=ROOMS_PAGE_2)
        solves = await adapter.get_recent_solves(USER, deep=True)
    assert [s.item_ref for s in solves] == ["mrrobot", "vulnversity", "blue"]


async def test_shallow_poll_reads_one_page(adapter):
    with aioresponses() as m:
        m.get(_rooms_url(1), payload=ROOMS_PAGE_1)  # only page 1 mocked
        solves = await adapter.get_recent_solves(USER, deep=False)
    assert len(solves) == 2


async def test_verification_reads_the_about_bio(adapter):
    """THM's `about` field is a real bio — this is what makes /verify possible."""
    payload = {"data": {**PROFILE_PAYLOAD["data"], "about": "hacking | hq-ab12cd34"}}
    with aioresponses() as m:
        m.get(_profile_url(), payload=payload)
        haystack = await adapter.get_verification_token_haystack(USER)
    assert "hq-ab12cd34" in haystack


async def test_challenge_degrades_instead_of_lying(adapter):
    with aioresponses() as m:
        m.get(_profile_url(), **CHALLENGE_KW)
        with pytest.raises(PlatformUnavailable, match="challenge"):
            await adapter.get_profile(USER)


async def test_dead_endpoint_serving_html_is_not_parsed(adapter):
    """Retired v1 endpoints answer 200 with the SPA's HTML — that must never
    be mistaken for an empty profile."""
    with aioresponses() as m:
        m.get(_profile_url(), status=200, body="<!DOCTYPE html><html>", content_type="text/html")
        with pytest.raises(PlatformUnavailable):
            await adapter.get_profile(USER)


async def test_solves_degrade_to_empty_on_challenge(adapter):
    with aioresponses() as m:
        m.get(_rooms_url(1), **CHALLENGE_KW)
        assert await adapter.get_recent_solves(USER) == []


async def test_unknown_user(adapter):
    with aioresponses() as m:
        m.get(_profile_url("ghost"), payload={"status": "error", "data": None})
        with pytest.raises(ProfileNotFound):
            await adapter.resolve_user("ghost")


async def test_resolve_accepts_profile_url_and_at_prefix(adapter):
    with aioresponses() as m:
        m.get(_profile_url(), payload=PROFILE_PAYLOAD, repeat=True)
        assert (await adapter.resolve_user("@neo")).user_id == "neo"
        assert (await adapter.resolve_user("https://tryhackme.com/p/neo")).user_id == "neo"
