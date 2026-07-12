from __future__ import annotations

import pytest

from hackqueue.config import ScoringConfig
from hackqueue.services.claims import ClaimError, ClaimsService

GUILD = 100
ALICE = 1


@pytest.fixture
def claims(db):
    return ClaimsService(db, ScoringConfig.defaults())


async def test_create_pending_claim_with_config_points(claims):
    claim = await claims.create(GUILD, ALICE, "pg", "Nibbles", "Easy", None)
    assert claim.status == "pending"
    assert claim.points == 10  # from defaults; difficulty case-insensitive


async def test_unknown_platform_rejected(claims):
    with pytest.raises(ClaimError, match="Unknown claim platform"):
        await claims.create(GUILD, ALICE, "nope", "Box", "easy", None)


async def test_unknown_difficulty_rejected_with_options(claims):
    with pytest.raises(ClaimError, match="easy"):
        await claims.create(GUILD, ALICE, "pg", "Box", "ultranightmare", None)


async def test_duplicate_pending_claim_rejected(claims):
    await claims.create(GUILD, ALICE, "pg", "Nibbles", "easy", None)
    with pytest.raises(ClaimError, match="pending"):
        await claims.create(GUILD, ALICE, "pg", "nibbles", "easy", None)  # case-insensitive


async def test_approve_awards_and_is_idempotent(claims):
    claim = await claims.create(GUILD, ALICE, "pg", "Nibbles", "hard", None)
    reviewed = await claims.review(claim.id, approve=True, reviewer_id=99)
    assert reviewed.status == "approved"
    assert reviewed.points == 30
    assert reviewed.reviewed_by == 99
    # double-click on the button must not re-review
    assert await claims.review(claim.id, approve=False, reviewer_id=99) is None


async def test_denied_claim_can_be_resubmitted(claims):
    claim = await claims.create(GUILD, ALICE, "pg", "Nibbles", "easy", None)
    await claims.review(claim.id, approve=False, reviewer_id=99)
    again = await claims.create(GUILD, ALICE, "pg", "Nibbles", "easy", None)
    assert again.status == "pending"


async def test_duplicate_check_treats_percent_as_literal(claims):
    """User input must never act as a LIKE pattern: a claim named '%' must not
    collide with every other claim."""
    await claims.create(GUILD, ALICE, "pg", "Nibbles", "easy", None)
    claim = await claims.create(GUILD, ALICE, "pg", "%", "easy", None)
    assert claim.status == "pending"


async def test_delete_removes_claim(claims):
    claim = await claims.create(GUILD, ALICE, "pg", "Nibbles", "easy", None)
    await claims.delete(claim.id)
    assert await claims.get(claim.id) is None
    # resubmission works — nothing orphaned
    again = await claims.create(GUILD, ALICE, "pg", "Nibbles", "easy", None)
    assert again.status == "pending"


async def test_purge_user_scoped_to_guild(claims):
    await claims.create(GUILD, ALICE, "pg", "Box1", "easy", None)
    await claims.create(GUILD, ALICE, "pg", "Box2", "easy", None)
    await claims.create(GUILD + 1, ALICE, "pg", "Box1", "easy", None)
    assert await claims.purge_user(GUILD, ALICE) == 2
    assert await claims.purge_user(GUILD, ALICE) == 0
    assert await claims.purge_user(GUILD + 1, ALICE) == 1
