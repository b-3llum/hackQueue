"""Account linking, unlinking (data purge), and ownership verification."""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import delete, select

from hackqueue.adapters.base import Platform, PlatformUser
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.db.models import AccountLink, utcnow
from hackqueue.db.repo import ensure_member
from hackqueue.db.session import Database
from hackqueue.log import get_logger

log = get_logger(__name__)

VERIFY_TOKEN_TTL = timedelta(hours=24)


class LinkError(Exception):
    """User-facing linking failure; message is shown verbatim in Discord."""


def link_to_platform_user(link: AccountLink) -> PlatformUser:
    return PlatformUser(
        platform=Platform(link.platform),
        user_id=link.platform_user_id,
        username=link.platform_username,
        extra_ids=dict(link.extra_ids or {}),
    )


class LinkingService:
    def __init__(self, db: Database, adapters: AdapterRegistry) -> None:
        self._db = db
        self._adapters = adapters

    async def link(
        self, guild_id: int, discord_user_id: int, platform: Platform, user_ref: str
    ) -> AccountLink:
        adapter = self._adapters.get(platform)
        if adapter is None:
            raise LinkError(
                f"`{platform}` is not enabled on this instance "
                "(the operator hasn't configured its API credential)."
            )
        # Validate against the live platform BEFORE touching the DB, so users
        # get immediate feedback on bad ids/private profiles.
        user = await adapter.resolve_user(user_ref)

        async with self._db.session() as session, session.begin():
            existing = await session.scalar(
                select(AccountLink).where(
                    AccountLink.discord_user_id == discord_user_id,
                    AccountLink.platform == platform.value,
                )
            )
            if existing is not None:
                raise LinkError(
                    f"You already have a {platform} account linked "
                    f"(**{existing.platform_username}**). `/unlink {platform}` first."
                )
            claimed = await session.scalar(
                select(AccountLink).where(
                    AccountLink.platform == platform.value,
                    AccountLink.platform_user_id == user.user_id,
                )
            )
            if claimed is not None:
                raise LinkError(
                    "That account is already linked by another Discord user. "
                    "If this is your account, ask a server admin to resolve it."
                )
            await ensure_member(session, guild_id, discord_user_id)
            link = AccountLink(
                discord_user_id=discord_user_id,
                platform=platform.value,
                platform_user_id=user.user_id,
                platform_username=user.username,
                extra_ids=user.extra_ids,
            )
            session.add(link)
            await session.flush()
            log.info(
                "account_linked",
                platform=platform.value,
                discord_user_id=discord_user_id,
                platform_user=user.username,
            )
            return link

    async def unlink(self, discord_user_id: int, platform: Platform) -> bool:
        """Delete the link. Snapshots and solves cascade — this is the
        user-facing data purge documented in the README."""
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                delete(AccountLink).where(
                    AccountLink.discord_user_id == discord_user_id,
                    AccountLink.platform == platform.value,
                )
            )
            purged = result.rowcount > 0
        if purged:
            log.info("account_unlinked", platform=platform.value, discord_user_id=discord_user_id)
        return purged

    async def register_member(self, guild_id: int, discord_user_id: int) -> None:
        async with self._db.session() as session, session.begin():
            await ensure_member(session, guild_id, discord_user_id)

    async def links_of(self, discord_user_id: int) -> list[AccountLink]:
        async with self._db.session() as session:
            result = await session.scalars(
                select(AccountLink).where(AccountLink.discord_user_id == discord_user_id)
            )
            return list(result)

    async def get_link(self, discord_user_id: int, platform: Platform) -> AccountLink | None:
        async with self._db.session() as session:
            return await session.scalar(
                select(AccountLink).where(
                    AccountLink.discord_user_id == discord_user_id,
                    AccountLink.platform == platform.value,
                )
            )

    async def start_or_check_verification(
        self, discord_user_id: int, platform: Platform
    ) -> tuple[str, str | None]:
        """One command, two phases. Returns (phase, token):
        ("issued", token)   — token (re)issued; the user pastes it into the
                              public field named by the adapter's instructions
        ("verified", None)  — the token was found; the link is now verified
        ("not_found", tok)  — field readable but no token yet; same token stands
        """
        adapter = self._adapters.get(platform)
        if adapter is None or not adapter.supports_verification:
            raise LinkError(f"`{platform}` doesn't support ownership verification.")
        link = await self.get_link(discord_user_id, platform)
        if link is None:
            raise LinkError(f"You have no {platform} account linked. `/link {platform}` first.")
        if link.verified:
            raise LinkError("This link is already verified. ✅")

        now = utcnow()
        if not link.verify_token or (link.verify_expires_at and link.verify_expires_at < now):
            token = "hq-" + secrets.token_hex(4)
            async with self._db.session() as session, session.begin():
                db_link = await session.get(AccountLink, link.id)
                assert db_link is not None
                db_link.verify_token = token
                db_link.verify_expires_at = now + VERIFY_TOKEN_TTL
            return "issued", token

        haystack = await adapter.get_verification_token_haystack(link_to_platform_user(link))
        if haystack is None:
            raise LinkError(
                f"{platform} isn't exposing a field the bot can check right now, "
                "so verification is unavailable. Your link still works unverified."
            )
        if link.verify_token in haystack:
            async with self._db.session() as session, session.begin():
                db_link = await session.get(AccountLink, link.id)
                assert db_link is not None
                db_link.verified = True
                db_link.verify_token = None
                db_link.verify_expires_at = None
            log.info("link_verified", platform=platform.value, discord_user_id=discord_user_id)
            return "verified", None
        return "not_found", link.verify_token
