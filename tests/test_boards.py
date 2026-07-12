from __future__ import annotations

from datetime import timedelta

import pytest

from hackqueue.adapters.base import Platform
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.config import ScoringConfig
from hackqueue.db.models import AccountLink, Claim, Guild, GuildMember, Snapshot, utcnow
from hackqueue.services.boards import BoardService
from hackqueue.services.health import HealthRegistry
from hackqueue.services.scoring import Period, period_start

GUILD = 100


class _StubAdapter:
    platform = Platform.HTB


@pytest.fixture
def registry():
    reg = AdapterRegistry()
    reg.register(_StubAdapter())  # type: ignore[arg-type]
    return reg


@pytest.fixture
def boards(db, registry):
    return BoardService(db, registry, HealthRegistry(), ScoringConfig.defaults())


async def seed(db, *, require_verified: bool = False):
    """Alice: 100 -> 150 across the week boundary (delta 50, verified).
    Bob: joined mid-week, 200 -> 220 (delta 20, unverified)."""
    week_start = period_start(Period.WEEKLY, utcnow())
    before = week_start - timedelta(days=1)
    mid = week_start + timedelta(hours=6)
    later = week_start + timedelta(hours=12)
    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=GUILD, require_verified=require_verified))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=1))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=2))
        alice = AccountLink(
            discord_user_id=1,
            platform="htb",
            platform_user_id="11",
            platform_username="alice",
            verified=True,
        )
        bob = AccountLink(
            discord_user_id=2,
            platform="htb",
            platform_user_id="22",
            platform_username="bob",
            verified=False,
        )
        session.add_all([alice, bob])
        await session.flush()
        session.add_all(
            [
                Snapshot(link_id=alice.id, taken_at=before, points=100, counters={}),
                Snapshot(link_id=alice.id, taken_at=later, points=150, counters={}),
                Snapshot(link_id=bob.id, taken_at=mid, points=200, counters={}),
                Snapshot(link_id=bob.id, taken_at=later, points=220, counters={}),
            ]
        )


async def test_weekly_platform_board_deltas(db, boards):
    await seed(db)
    board = await boards.platform_board(GUILD, Platform.HTB, Period.WEEKLY)
    assert [(r.discord_user_id, r.value) for r in board.rows] == [(1, 50.0), (2, 20.0)]
    assert board.rows[0].verified is True
    assert board.rows[1].verified is False


async def test_alltime_platform_board_raw_points(db, boards):
    await seed(db)
    board = await boards.platform_board(GUILD, Platform.HTB, Period.ALLTIME)
    assert [(r.discord_user_id, r.value) for r in board.rows] == [(2, 220.0), (1, 150.0)]


async def test_require_verified_hides_unverified(db, boards):
    await seed(db, require_verified=True)
    board = await boards.platform_board(GUILD, Platform.HTB, Period.WEEKLY)
    assert [r.discord_user_id for r in board.rows] == [1]


async def test_composite_includes_claims(db, boards):
    await seed(db)
    async with db.session() as session, session.begin():
        session.add(
            Claim(
                guild_id=GUILD,
                discord_user_id=2,
                platform_key="pg",
                item_name="Nibbles",
                difficulty="easy",
                points=10,
                status="approved",
                reviewed_by=99,
                reviewed_at=utcnow(),
            )
        )
    board = await boards.composite_board(GUILD, Period.WEEKLY)
    scores = {r.discord_user_id: r.value for r in board.rows}
    # htb: alice 100, bob 40 (normalized). claims: bob 100, alice 0.
    # composite (equal weights): alice 50, bob 70.
    assert scores[1] == pytest.approx(50.0)
    assert scores[2] == pytest.approx(70.0)
    assert board.rows[0].discord_user_id == 2  # bob leads


async def test_claims_board_respects_period(db, boards):
    week_start = period_start(Period.WEEKLY, utcnow())
    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=GUILD))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=1))
        await session.flush()
        for name, reviewed, pts in (
            ("OldBox", week_start - timedelta(days=2), 30),
            ("NewBox", week_start + timedelta(hours=1), 20),
        ):
            session.add(
                Claim(
                    guild_id=GUILD,
                    discord_user_id=1,
                    platform_key="pg",
                    item_name=name,
                    difficulty="easy",
                    points=pts,
                    status="approved",
                    reviewed_by=9,
                    reviewed_at=reviewed,
                )
            )
    weekly = await boards.claims_board(GUILD, Period.WEEKLY)
    alltime = await boards.claims_board(GUILD, Period.ALLTIME)
    assert weekly.rows[0].value == 20.0
    assert alltime.rows[0].value == 50.0


async def test_pending_and_denied_claims_do_not_score(db, boards):
    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=GUILD))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=1))
        await session.flush()
        for status in ("pending", "denied"):
            session.add(
                Claim(
                    guild_id=GUILD,
                    discord_user_id=1,
                    platform_key="pg",
                    item_name=f"box-{status}",
                    difficulty="easy",
                    points=10,
                    status=status,
                    reviewed_at=utcnow() if status == "denied" else None,
                )
            )
    board = await boards.claims_board(GUILD, Period.ALLTIME)
    assert board.rows == []


async def test_empty_guild_board(db, boards):
    board = await boards.platform_board(GUILD, Platform.HTB, Period.WEEKLY)
    assert board.rows == []


async def test_as_of_windowing_ignores_later_snapshots(db, boards):
    """Anchoring a board in the past (the recap's completed-week view) must
    ignore snapshots taken after the anchor."""
    await seed(db)
    week_start = period_start(Period.WEEKLY, utcnow())
    board = await boards.platform_board(
        GUILD, Platform.HTB, Period.WEEKLY, as_of=week_start + timedelta(hours=1)
    )
    # at +1h, alice has baseline(100) but her +12h snapshot is invisible; bob's
    # first snapshot is at +6h — also invisible. Nobody has gains yet.
    assert board.rows == []
    # at +13h both later snapshots are visible again
    board = await boards.platform_board(
        GUILD, Platform.HTB, Period.WEEKLY, as_of=week_start + timedelta(hours=13)
    )
    assert [(r.discord_user_id, r.value) for r in board.rows] == [(1, 50.0), (2, 20.0)]


async def test_claim_totals_exclude_hidden_and_non_members(db, boards):
    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=GUILD))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=1))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=2, hidden=True))
        await session.flush()
        for uid in (1, 2, 3):  # 3 has claims but never joined the guild boards
            session.add(
                Claim(
                    guild_id=GUILD,
                    discord_user_id=uid,
                    platform_key="pg",
                    item_name=f"box-{uid}",
                    difficulty="easy",
                    points=10,
                    status="approved",
                    reviewed_by=9,
                    reviewed_at=utcnow(),
                )
            )
    board = await boards.claims_board(GUILD, Period.ALLTIME)
    assert [r.discord_user_id for r in board.rows] == [1]
