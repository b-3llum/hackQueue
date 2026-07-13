from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hackqueue.adapters.base import Platform
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.config import ScoringConfig
from hackqueue.db.models import AccountLink, Claim, Guild, GuildMember, Snapshot
from hackqueue.services.boards import BoardService
from hackqueue.services.health import HealthRegistry
from hackqueue.services.scoring import Period, period_start

GUILD = 100

# Every board call passes an explicit `as_of`, so these tests are pinned to a
# fixed instant and never depend on the wall clock. (Seeding relative to
# utcnow() used to fail every Monday between 00:00 and 12:00 UTC, when
# "week start + 12h" lands in the future and is correctly filtered out.)
NOW = datetime(2026, 3, 5, 13, 0, tzinfo=UTC)  # a Thursday
WEEK_START = period_start(Period.WEEKLY, NOW)  # Monday 2026-03-02 00:00 UTC


class _StubAdapter:
    platform = Platform.HTB
    supports_verification = True  # HTB can be verified (social-link token)


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
    before = WEEK_START - timedelta(days=1)
    mid = WEEK_START + timedelta(hours=6)
    later = WEEK_START + timedelta(hours=12)
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
    board = await boards.platform_board(GUILD, Platform.HTB, Period.WEEKLY, as_of=NOW)
    assert [(r.discord_user_id, r.value) for r in board.rows] == [(1, 50.0), (2, 20.0)]
    assert board.rows[0].verified is True
    assert board.rows[1].verified is False
    assert board.rows[0].parts == {"htb": 50.0}


async def test_alltime_platform_board_raw_points(db, boards):
    await seed(db)
    board = await boards.platform_board(GUILD, Platform.HTB, Period.ALLTIME, as_of=NOW)
    assert [(r.discord_user_id, r.value) for r in board.rows] == [(2, 220.0), (1, 150.0)]


async def test_require_verified_hides_unverified(db, boards):
    await seed(db, require_verified=True)
    board = await boards.platform_board(GUILD, Platform.HTB, Period.WEEKLY, as_of=NOW)
    assert [r.discord_user_id for r in board.rows] == [1]


async def test_composite_includes_claims_and_exposes_contributions(db, boards):
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
                reviewed_at=WEEK_START + timedelta(hours=8),
            )
        )
    board = await boards.composite_board(GUILD, Period.WEEKLY, as_of=NOW)
    scores = {r.discord_user_id: r.value for r in board.rows}
    # htb: alice 100, bob 40 (normalized). claims: bob 100, alice 0.
    # composite (equal weights over the participating platforms): alice 50, bob 70.
    assert scores[1] == pytest.approx(50.0)
    assert scores[2] == pytest.approx(70.0)
    assert board.rows[0].discord_user_id == 2  # bob leads

    # The stacked-bar contract: parts sum to the score and name their platform.
    bob_row = next(r for r in board.rows if r.discord_user_id == 2)
    assert sum(bob_row.parts.values()) == pytest.approx(bob_row.value)
    assert bob_row.parts["claims"] == pytest.approx(50.0)
    assert bob_row.parts["htb"] == pytest.approx(20.0)


async def test_as_of_windowing_ignores_later_snapshots(db, boards):
    """Anchoring a board in the past (what the recap does for the completed
    week) must ignore snapshots taken after the anchor."""
    await seed(db)
    early = await boards.platform_board(
        GUILD, Platform.HTB, Period.WEEKLY, as_of=WEEK_START + timedelta(hours=1)
    )
    assert early.rows == []  # the +6h and +12h snapshots aren't visible yet
    later = await boards.platform_board(
        GUILD, Platform.HTB, Period.WEEKLY, as_of=WEEK_START + timedelta(hours=13)
    )
    assert [(r.discord_user_id, r.value) for r in later.rows] == [(1, 50.0), (2, 20.0)]


async def test_claims_board_respects_period(db, boards):
    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=GUILD))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=1))
        await session.flush()
        for name, reviewed, pts in (
            ("OldBox", WEEK_START - timedelta(days=2), 30),
            ("NewBox", WEEK_START + timedelta(hours=1), 20),
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
    weekly = await boards.claims_board(GUILD, Period.WEEKLY, as_of=NOW)
    alltime = await boards.claims_board(GUILD, Period.ALLTIME, as_of=NOW)
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
                    reviewed_at=WEEK_START if status == "denied" else None,
                )
            )
    board = await boards.claims_board(GUILD, Period.ALLTIME, as_of=NOW)
    assert board.rows == []


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
                    reviewed_at=WEEK_START + timedelta(hours=2),
                )
            )
    board = await boards.claims_board(GUILD, Period.ALLTIME, as_of=NOW)
    assert [r.discord_user_id for r in board.rows] == [1]


async def test_empty_guild_board(db, boards):
    board = await boards.platform_board(GUILD, Platform.HTB, Period.WEEKLY, as_of=NOW)
    assert board.rows == []


class _RootMeStub:
    """Root-Me exposes nothing the bot can check — verification is impossible."""

    platform = Platform.ROOTME
    supports_verification = False


class _HTBStub:
    platform = Platform.HTB
    supports_verification = True


@pytest.fixture
def mixed_registry():
    reg = AdapterRegistry()
    reg.register(_HTBStub())  # type: ignore[arg-type]
    reg.register(_RootMeStub())  # type: ignore[arg-type]
    return reg


@pytest.fixture
def mixed_boards(db, mixed_registry):
    return BoardService(db, mixed_registry, HealthRegistry(), ScoringConfig.defaults())


async def seed_mixed(db, *, require_verified: bool = False):
    """One member: HTB verified, Root-Me unverifiable (it can never be verified)."""
    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=GUILD, require_verified=require_verified))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=1))
        htb = AccountLink(
            discord_user_id=1,
            platform="htb",
            platform_user_id="11",
            platform_username="xb3llum",
            verified=True,
        )
        rootme = AccountLink(
            discord_user_id=1,
            platform="rootme",
            platform_user_id="22",
            platform_username="xbellum",
            verified=False,  # and it can never become True
        )
        session.add_all([htb, rootme])
        await session.flush()
        for link in (htb, rootme):
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
                        points=150,
                        counters={},
                    ),
                ]
            )


async def test_unverifiable_platform_is_not_flagged(db, mixed_boards):
    """Root-Me can't do verification, so its rows must not wear the ⚠ marker —
    the member did nothing wrong."""
    await seed_mixed(db)
    board = await mixed_boards.platform_board(GUILD, Platform.ROOTME, Period.WEEKLY, as_of=NOW)
    assert board.rows[0].verified is True


async def test_unverifiable_link_does_not_taint_composite(db, mixed_boards):
    """A permanently-unverifiable Root-Me link must not make a member with a
    verified HTB link show as unverified forever."""
    await seed_mixed(db)
    board = await mixed_boards.composite_board(GUILD, Period.WEEKLY, as_of=NOW)
    assert board.rows[0].verified is True


async def test_verifiable_but_unverified_still_flags_composite(db, mixed_boards):
    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=GUILD))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=1))
        link = AccountLink(
            discord_user_id=1,
            platform="htb",
            platform_user_id="11",
            platform_username="x",
            verified=False,  # HTB *can* be verified, so this one counts
        )
        session.add(link)
        await session.flush()
        session.add_all(
            [
                Snapshot(
                    link_id=link.id,
                    taken_at=WEEK_START - timedelta(days=1),
                    points=10,
                    counters={},
                ),
                Snapshot(
                    link_id=link.id,
                    taken_at=WEEK_START + timedelta(hours=2),
                    points=90,
                    counters={},
                ),
            ]
        )
    board = await mixed_boards.composite_board(GUILD, Period.WEEKLY, as_of=NOW)
    assert board.rows[0].verified is False


async def test_require_verified_keeps_unverifiable_platforms_on_the_board(db, mixed_boards):
    """require_verified must not silently empty the Root-Me/THM boards — no link
    there could ever satisfy it."""
    await seed_mixed(db, require_verified=True)
    rootme = await mixed_boards.platform_board(GUILD, Platform.ROOTME, Period.WEEKLY, as_of=NOW)
    htb = await mixed_boards.platform_board(GUILD, Platform.HTB, Period.WEEKLY, as_of=NOW)
    assert [r.label for r in rootme.rows] == ["xbellum"]  # still shown
    assert [r.label for r in htb.rows] == ["xb3llum"]  # verified, so also shown
