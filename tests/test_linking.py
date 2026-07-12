from __future__ import annotations

from typing import ClassVar

import pytest
from sqlalchemy import func, select

from hackqueue.adapters.base import Platform, PlatformAdapter, PlatformUser, ProfileStats
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.db.models import AccountLink, Snapshot, Solve
from hackqueue.services.linking import LinkError, LinkingService

GUILD = 100
ALICE = 1
BOB = 2


class FakeHTB(PlatformAdapter):
    platform: ClassVar[Platform] = Platform.HTB
    supports_verification: ClassVar[bool] = True

    def __init__(self) -> None:
        self.bio = ""

    async def resolve_user(self, user_ref: str) -> PlatformUser:
        return PlatformUser(platform=Platform.HTB, user_id=user_ref, username=f"user{user_ref}")

    async def get_profile(self, user: PlatformUser) -> ProfileStats:
        return ProfileStats(
            platform=Platform.HTB, user_id=user.user_id, username=user.username, points=1, rank=1
        )

    async def get_recent_solves(self, user):
        return []

    async def get_verification_bio(self, user) -> str | None:
        return self.bio


@pytest.fixture
def registry():
    reg = AdapterRegistry()
    reg.register(FakeHTB())
    return reg


@pytest.fixture
def linking(db, registry):
    return LinkingService(db, registry)


async def test_link_creates_link_and_membership(db, linking):
    link = await linking.link(GUILD, ALICE, Platform.HTB, "1337")
    assert link.platform_username == "user1337"
    async with db.session() as session:
        assert await session.scalar(select(func.count()).select_from(AccountLink)) == 1


async def test_double_link_same_platform_rejected(linking):
    await linking.link(GUILD, ALICE, Platform.HTB, "1337")
    with pytest.raises(LinkError, match="already have"):
        await linking.link(GUILD, ALICE, Platform.HTB, "9999")


async def test_account_claimed_by_other_user_rejected(linking):
    await linking.link(GUILD, ALICE, Platform.HTB, "1337")
    with pytest.raises(LinkError, match="another Discord user"):
        await linking.link(GUILD, BOB, Platform.HTB, "1337")


async def test_disabled_platform_rejected(linking):
    with pytest.raises(LinkError, match="not enabled"):
        await linking.link(GUILD, ALICE, Platform.ROOTME, "5")


async def test_unlink_purges_snapshots_and_solves(db, linking):
    """The privacy guarantee: /unlink cascades. Also exercises the SQLite
    foreign_keys pragma — without it this test fails with orphaned rows."""
    link = await linking.link(GUILD, ALICE, Platform.HTB, "1337")
    async with db.session() as session, session.begin():
        session.add(Snapshot(link_id=link.id, points=10, rank=1, counters={}))
        session.add(
            Solve(link_id=link.id, platform="htb", item_ref="1", item_name="Lame", kind="root")
        )
    assert await linking.unlink(ALICE, Platform.HTB) is True
    async with db.session() as session:
        assert await session.scalar(select(func.count()).select_from(Snapshot)) == 0
        assert await session.scalar(select(func.count()).select_from(Solve)) == 0


async def test_unlink_nothing_linked(linking):
    assert await linking.unlink(ALICE, Platform.HTB) is False


async def test_verification_flow(linking, registry):
    await linking.link(GUILD, ALICE, Platform.HTB, "1337")
    phase, token = await linking.start_or_check_verification(ALICE, Platform.HTB)
    assert phase == "issued" and token.startswith("hq-")

    # token not in bio yet
    phase, same_token = await linking.start_or_check_verification(ALICE, Platform.HTB)
    assert phase == "not_found" and same_token == token

    fake: FakeHTB = registry.get(Platform.HTB)
    fake.bio = f"pwning boxes | {token} | he/him"
    phase, _ = await linking.start_or_check_verification(ALICE, Platform.HTB)
    assert phase == "verified"

    link = await linking.get_link(ALICE, Platform.HTB)
    assert link.verified is True
    assert link.verify_token is None

    with pytest.raises(LinkError, match="already verified"):
        await linking.start_or_check_verification(ALICE, Platform.HTB)


async def test_verification_requires_link(linking):
    with pytest.raises(LinkError, match=r"no .* linked|no htb"):
        await linking.start_or_check_verification(ALICE, Platform.HTB)
