from __future__ import annotations

from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hackqueue.adapters.base import Platform
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.config import ScoringConfig, Settings
from hackqueue.db.models import AccountLink, Claim, Guild, GuildMember, Snapshot
from hackqueue.services.boards import BoardService
from hackqueue.services.directory import MemberIdentity
from hackqueue.services.health import HealthRegistry
from hackqueue.web.server import WebServer

from .test_boards import WEEK_START

GUILD = 555


class _StubAdapter:
    platform = Platform.HTB


class _StubDirectory:
    """The real one calls Discord; the web layer only needs names back."""

    async def identities(self, guild_id: int, user_ids: list[int]):
        return {
            uid: MemberIdentity(display_name=f"member{uid}", avatar_url=f"https://cdn/{uid}.png")
            for uid in user_ids
        }


class _StubClient:
    def get_guild(self, guild_id: int):
        return None  # not in the bot's cache; the server must cope


@pytest.fixture
async def client(db):
    registry = AdapterRegistry()
    registry.register(_StubAdapter())  # type: ignore[arg-type]
    boards = BoardService(db, registry, HealthRegistry(), ScoringConfig.defaults())
    settings = Settings(discord_token="x", web_enabled=True, _env_file=None)
    server = WebServer(settings, db, boards, _StubDirectory(), _StubClient())  # type: ignore[arg-type]
    async with TestClient(TestServer(server.build_app())) as client:
        yield client


async def seed(db, *, web_enabled: bool):
    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=GUILD, web_enabled=web_enabled))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=1))
        link = AccountLink(
            discord_user_id=1,
            platform="htb",
            platform_user_id="11",
            platform_username="alice",
            verified=False,
        )
        session.add(link)
        await session.flush()
        session.add_all(
            [
                Snapshot(
                    link_id=link.id,
                    taken_at=WEEK_START - timedelta(days=1),
                    points=100,
                    counters={},
                ),
                Snapshot(
                    link_id=link.id,
                    taken_at=WEEK_START + timedelta(hours=2),
                    points=140,
                    counters={},
                ),
            ]
        )
        session.add(
            Claim(
                guild_id=GUILD,
                discord_user_id=1,
                platform_key="pg",
                item_name="Nibbles",
                difficulty="easy",
                points=10,
                status="approved",
                reviewed_by=9,
                reviewed_at=WEEK_START + timedelta(hours=3),
            )
        )


async def test_unpublished_guild_is_not_served(db, client):
    """The privacy contract: no opt-in, no page — and no names leak via the API."""
    await seed(db, web_enabled=False)
    assert (await client.get(f"/g/{GUILD}")).status == 404
    api = await client.get(f"/api/g/{GUILD}")
    assert api.status == 404
    assert (await api.json())["error"] == "not_published"


async def test_unknown_guild_404s(client):
    assert (await client.get("/api/g/123456")).status == 404
    assert (await client.get("/api/g/not-a-number")).status == 404


async def test_published_board_renders_rows(db, client):
    await seed(db, web_enabled=True)
    res = await client.get(f"/api/g/{GUILD}?board=htb&period=alltime")
    assert res.status == 200
    body = await res.json()
    assert body["board"] == "htb"
    row = body["rows"][0]
    assert row["rank"] == 1
    assert row["name"] == "member1"
    assert row["handle"] == "alice"  # platform username
    assert row["value"] == 140
    assert row["verified"] is False
    assert (await client.get(f"/g/{GUILD}")).status == 200


async def test_composite_rows_carry_stacked_parts(db, client):
    """The bar is the formula: parts name their platform and sum to the score."""
    await seed(db, web_enabled=True)
    body = await (await client.get(f"/api/g/{GUILD}?board=composite&period=alltime")).json()
    row = body["rows"][0]
    assert set(row["parts"]) == {"htb", "claims"}
    assert sum(row["parts"].values()) == pytest.approx(row["value"], abs=0.05)


async def test_bad_period_and_board_rejected(db, client):
    await seed(db, web_enabled=True)
    assert (await client.get(f"/api/g/{GUILD}?period=yearly")).status == 400
    assert (await client.get(f"/api/g/{GUILD}?board=nope")).status == 400


async def test_healthz(client):
    body = await (await client.get("/healthz")).json()
    assert body["ok"] is True
