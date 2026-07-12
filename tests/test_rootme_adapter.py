from __future__ import annotations

import pytest
from aioresponses import aioresponses

from hackqueue.adapters.base import AuthExpired, Platform, PlatformUser, ProfileNotFound
from hackqueue.adapters.rootme import URL_AUTHOR, RootMeAdapter

USER = PlatformUser(platform=Platform.ROOTME, user_id="222", username="g0uZ")

# Real-world shape: numbers as strings, validations as numeric-key dict.
AUTHOR_PAYLOAD = [
    {
        "nom": "g0uZ",
        "score": "3450",
        "position": "1337",
        "rang": "elite",
        "validations": {
            "0": {
                "id_challenge": "5",
                "titre": "HTML - source code",
                "date": "2026-07-01 10:00:00",
            },
            "1": {"id_challenge": "9", "titre": "Weak password"},
        },
    }
]


@pytest.fixture
def adapter(http):
    return RootMeAdapter(http, api_key="k3y")


async def test_profile_parses_strings_and_array_wrapper(adapter):
    with aioresponses() as m:
        m.get(URL_AUTHOR.format(author_id="222"), payload=AUTHOR_PAYLOAD)
        stats = await adapter.get_profile(USER)
    assert stats.points == 3450
    assert stats.rank == 1337
    assert stats.counters == {"validations": 2}


async def test_solves_from_numeric_key_validations(adapter):
    with aioresponses() as m:
        m.get(URL_AUTHOR.format(author_id="222"), payload=AUTHOR_PAYLOAD)
        solves = await adapter.get_recent_solves(USER)
    assert [s.item_ref for s in solves] == ["5", "9"]
    assert solves[0].name == "HTML - source code"
    assert solves[0].solved_at is not None
    assert solves[1].solved_at is None  # missing date tolerated


async def test_poll_hits_api_once(adapter):
    with aioresponses() as m:
        m.get(URL_AUTHOR.format(author_id="222"), payload=AUTHOR_PAYLOAD)
        # One registered mock: a second request would fail the test.
        stats, solves = await adapter.poll(USER)
    assert stats.points == 3450
    assert len(solves) == 2


async def test_401_array_wrapped_error_raises_auth_expired(adapter):
    with aioresponses() as m:
        m.get(
            URL_AUTHOR.format(author_id="222"),
            status=401,
            payload=[{"error": {"code": 401, "message": "Error 401"}}],
        )
        with pytest.raises(AuthExpired):
            await adapter.get_profile(USER)


async def test_resolve_rejects_non_numeric(adapter):
    with pytest.raises(ProfileNotFound, match="author ID"):
        await adapter.resolve_user("g0uZ")


async def test_search_by_name_parses_numeric_keys(adapter):
    payload = [
        {"0": {"id_auteur": "222", "nom": "g0uZ"}, "1": {"id_auteur": "999", "nom": "g0uZmate"}},
        [{"rel": "next"}],
    ]
    with aioresponses() as m:
        m.get("https://api.www.root-me.org/auteurs?nom=g0uZ", payload=payload)
        matches = await adapter.search_by_name("g0uZ")
    assert matches == [("222", "g0uZ"), ("999", "g0uZmate")]


async def test_validations_as_plain_list_also_works(adapter):
    payload = [{"nom": "x", "score": "10", "position": "5", "validations": [{"id_challenge": "7"}]}]
    with aioresponses() as m:
        m.get(URL_AUTHOR.format(author_id="222"), payload=payload)
        solves = await adapter.get_recent_solves(USER)
    assert [s.item_ref for s in solves] == ["7"]
