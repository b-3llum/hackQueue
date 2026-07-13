from __future__ import annotations

import discord
import pytest
from aioresponses import aioresponses

from hackqueue.adapters.htb import (
    URL_SEASON_LIST,
    URL_SEASON_MACHINE_ACTIVE,
    URL_SEASON_MACHINES,
    HTBAdapter,
)
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.db.repo import kv_get
from hackqueue.services.dropwatch import KV_LAST_DROP, DropWatchService
from hackqueue.services.seasons import SeasonService

from .test_seasons import MACHINE_ACTIVE, SEASON_LIST, SEASON_MACHINES


class _FakeChannel(discord.abc.Messageable):
    def __init__(self, sink):
        self._sink = sink

    async def _get_channel(self):  # abstract on Messageable
        return self

    async def send(self, *, embed=None, **_):
        self._sink.append(embed.title)


class _RecordingClient:
    """Stands in for the Discord client; records what would be announced."""

    def __init__(self):
        self.sent = []

    def get_channel(self, cid):
        return _FakeChannel(self.sent) if cid else None


@pytest.fixture
def watch(db, http):
    reg = AdapterRegistry()
    reg.register(HTBAdapter(http, "token"))
    return DropWatchService(_RecordingClient(), db, SeasonService(db, reg))


def _mock(m):
    m.get(URL_SEASON_LIST, payload=SEASON_LIST)
    m.get(URL_SEASON_MACHINES.format(season_id=15), payload=SEASON_MACHINES)
    m.get(URL_SEASON_MACHINE_ACTIVE, payload=MACHINE_ACTIVE)


async def test_first_run_primes_without_announcing(db, watch):
    """A fresh DB must not announce the current box as if it just dropped."""
    with aioresponses() as m:
        _mock(m)
        await watch._tick()
    assert watch._client.sent == []
    async with db.session() as session:
        assert (await kv_get(session, KV_LAST_DROP))["id"] == "921"


async def test_new_drop_is_announced_once(db, watch):
    from hackqueue.db.models import Guild
    from hackqueue.db.repo import kv_set

    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=1, announce_channel_id=555))
        await kv_set(session, KV_LAST_DROP, {"id": "918"})  # last announced was Enigma
    with aioresponses() as m:
        _mock(m)
        await watch._tick()
        _mock(m)
        await watch._tick()  # second tick: same machine, must not re-announce
    assert watch._client.sent == ["📦 New drop: Paperwork"]
