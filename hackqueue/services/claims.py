"""Manual solve claims (Proving Grounds and any other config-defined platform).

A claim platform is purely configuration (scoring.toml `[claims.<key>]`):
adding VulnHub or PortSwigger labs later touches no code. Claims are per-guild
and award their configured points only once a moderator approves them.
"""

from __future__ import annotations

from sqlalchemy import select

from hackqueue.config import ClaimPlatformConfig, ScoringConfig
from hackqueue.db.models import Claim, utcnow
from hackqueue.db.repo import ensure_member
from hackqueue.db.session import Database
from hackqueue.log import get_logger

log = get_logger(__name__)


class ClaimError(Exception):
    """User-facing claim failure; message shown verbatim in Discord."""


class ClaimsService:
    def __init__(self, db: Database, scoring: ScoringConfig) -> None:
        self._db = db
        self._scoring = scoring

    @property
    def platforms(self) -> dict[str, ClaimPlatformConfig]:
        return self._scoring.claims

    def platform(self, key: str) -> ClaimPlatformConfig | None:
        return self._scoring.claims.get(key)

    async def create(
        self,
        guild_id: int,
        discord_user_id: int,
        platform_key: str,
        item_name: str,
        difficulty: str,
        proof_url: str | None,
    ) -> Claim:
        cfg = self.platform(platform_key)
        if cfg is None:
            raise ClaimError(f"Unknown claim platform `{platform_key}`.")
        difficulty = difficulty.lower()
        if difficulty not in cfg.points:
            raise ClaimError(
                f"Unknown difficulty `{difficulty}` for {cfg.name}. "
                f"Options: {', '.join(sorted(cfg.points))}."
            )
        item_name = item_name.strip()
        if not item_name:
            raise ClaimError("Give the box/lab a name.")
        async with self._db.session() as session, session.begin():
            duplicate = await session.scalar(
                select(Claim).where(
                    Claim.guild_id == guild_id,
                    Claim.discord_user_id == discord_user_id,
                    Claim.platform_key == platform_key,
                    Claim.item_name.ilike(item_name),
                    Claim.status.in_(["pending", "approved"]),
                )
            )
            if duplicate is not None:
                state = "pending review" if duplicate.status == "pending" else "already approved"
                raise ClaimError(f"You have a claim for **{item_name}** that is {state}.")
            await ensure_member(session, guild_id, discord_user_id)
            claim = Claim(
                guild_id=guild_id,
                discord_user_id=discord_user_id,
                platform_key=platform_key,
                item_name=item_name,
                difficulty=difficulty,
                points=cfg.points[difficulty],
                proof_url=proof_url,
            )
            session.add(claim)
            await session.flush()
            log.info(
                "claim_created",
                claim_id=claim.id,
                guild_id=guild_id,
                platform=platform_key,
                item=item_name,
            )
            return claim

    async def set_message(self, claim_id: int, message_id: int) -> None:
        async with self._db.session() as session, session.begin():
            claim = await session.get(Claim, claim_id)
            if claim is not None:
                claim.mod_message_id = message_id

    async def review(self, claim_id: int, *, approve: bool, reviewer_id: int) -> Claim | None:
        """Approve/deny a pending claim. Returns None when it was already
        reviewed (double-clicked buttons must not double-award points)."""
        async with self._db.session() as session, session.begin():
            claim = await session.get(Claim, claim_id)
            if claim is None or claim.status != "pending":
                return None
            claim.status = "approved" if approve else "denied"
            claim.reviewed_by = reviewer_id
            claim.reviewed_at = utcnow()
            log.info("claim_reviewed", claim_id=claim_id, status=claim.status)
            return claim

    async def get(self, claim_id: int) -> Claim | None:
        async with self._db.session() as session:
            return await session.get(Claim, claim_id)
