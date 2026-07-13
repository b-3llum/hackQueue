from __future__ import annotations

from datetime import timedelta

import pytest

from hackqueue.adapters.base import Platform
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.db.models import AccountLink, CatalogBox, Claim, Guild, GuildMember, Snapshot, Solve
from hackqueue.services.profiles import ProfileService, _streak

from .test_boards import NOW, WEEK_START

GUILD = 700
ALICE = 1


class _HTB:
    platform = Platform.HTB
    supports_verification = True


class _RootMe:
    platform = Platform.ROOTME
    supports_verification = False


@pytest.fixture
def profiles(db):
    reg = AdapterRegistry()
    reg.register(_HTB())  # type: ignore[arg-type]
    reg.register(_RootMe())  # type: ignore[arg-type]
    return ProfileService(db, reg)


async def seed(db):
    async with db.session() as session, session.begin():
        session.add(Guild(guild_id=GUILD))
        session.add(GuildMember(guild_id=GUILD, discord_user_id=ALICE))
        session.add(
            CatalogBox(
                platform="htb",
                platform_ref="99",
                name="Cap",
                name_normalized="cap",
                url="https://app.hackthebox.com/machines/99",
            )
        )
        htb = AccountLink(
            discord_user_id=ALICE,
            platform="htb",
            platform_user_id="1918518",
            platform_username="xb3llum",
            verified=True,
        )
        rootme = AccountLink(
            discord_user_id=ALICE,
            platform="rootme",
            platform_user_id="1102101",
            platform_username="xbellum",
            verified=False,
        )
        session.add_all([htb, rootme])
        await session.flush()
        session.add_all(
            [
                Snapshot(
                    link_id=htb.id,
                    taken_at=NOW - timedelta(days=8),
                    points=30,
                    rank=2000,
                    counters={
                        "user_owns": 2,
                        "system_owns": 2,
                        "prolab_flags": 27,
                        "prolabs_completed": 1,
                    },
                ),
                Snapshot(
                    link_id=htb.id,
                    taken_at=NOW - timedelta(hours=1),
                    points=41,
                    rank=1032,
                    counters={
                        "user_owns": 4,
                        "system_owns": 4,
                        "prolab_flags": 33,
                        "prolabs_completed": 1,
                    },
                ),
                Snapshot(
                    link_id=rootme.id,
                    taken_at=NOW - timedelta(hours=1),
                    points=3005,
                    rank=3628,
                    counters={"validations": 123},
                ),
                # solves: one this week (real), one backfilled at link time
                Solve(
                    link_id=htb.id,
                    platform="htb",
                    item_ref="99",
                    item_name="Cap",
                    kind="root",
                    solved_at=NOW - timedelta(days=1),
                    first_seen_at=NOW - timedelta(days=1),
                    first_blood=True,
                ),
                Solve(
                    link_id=htb.id,
                    platform="htb",
                    item_ref="12",
                    item_name="Lame",
                    kind="user",
                    solved_at=NOW - timedelta(days=300),
                    first_seen_at=NOW - timedelta(days=1),
                    backfilled=True,
                ),
            ]
        )
        session.add(
            Claim(
                guild_id=GUILD,
                discord_user_id=ALICE,
                platform_key="pg",
                item_name="Nibbles",
                difficulty="hard",
                points=30,
                status="approved",
                reviewed_by=9,
                reviewed_at=WEEK_START + timedelta(hours=2),
            )
        )


async def test_member_detail_covers_every_platform(db, profiles):
    await seed(db)
    detail = await profiles.member(GUILD, ALICE, as_of=NOW)
    by_platform = {p.platform: p for p in detail.platforms}

    htb = by_platform["htb"]
    assert htb.score == 41
    assert htb.rank == 1032
    assert htb.weekly_gain == 11  # 41 - 30 baseline
    assert htb.verified is True and htb.verifiable is True
    assert htb.counters["prolab_flags"] == 33
    assert len(htb.series) == 2  # sparkline data
    assert htb.profile_url.endswith("/profile/1918518")

    rootme = by_platform["rootme"]
    assert rootme.score == 3005
    assert rootme.verifiable is False  # can't be verified — must not read as a failure


async def test_recent_solves_link_to_the_box(db, profiles):
    await seed(db)
    detail = await profiles.member(GUILD, ALICE, as_of=NOW)
    cap = next(s for s in detail.recent_solves if s.name == "Cap")
    assert cap.first_blood is True
    assert cap.url == "https://app.hackthebox.com/machines/99"
    assert detail.total_solves == 2


async def test_activity_excludes_backfilled_solves(db, profiles):
    """A member's imported pre-link history must not paint a fake wall of
    activity in the week they joined."""
    await seed(db)
    detail = await profiles.member(GUILD, ALICE, as_of=NOW)
    assert len(detail.activity) == 12
    assert sum(w["solves"] for w in detail.activity) == 1  # the Cap root own only


async def test_claims_are_counted(db, profiles):
    await seed(db)
    detail = await profiles.member(GUILD, ALICE, as_of=NOW)
    assert detail.claims_approved == 1
    assert detail.claims_points == 30


async def test_unknown_member_is_none(db, profiles):
    await seed(db)
    assert await profiles.member(GUILD, 999, as_of=NOW) is None


@pytest.mark.parametrize(
    ("weeks", "expected"),
    [
        ([1, 1, 1], 3),
        ([1, 0, 1], 1),
        ([0, 0, 0], 0),
        ([1, 2, 0], 2),  # the current week being empty doesn't break a streak
        ([], 0),
    ],
)
def test_streak(weeks, expected):
    assert _streak([{"week": str(i), "solves": n} for i, n in enumerate(weeks)]) == expected
