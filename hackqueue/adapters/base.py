"""Platform adapter contract and the normalized types every adapter returns.

Adding a platform = one new module implementing ``PlatformAdapter`` plus a
registry entry in ``registry.py``. Nothing outside ``adapters/`` may talk to a
platform API directly, and nothing inside an adapter may leak platform-specific
response shapes: everything is normalized to the dataclasses below, and every
failure is mapped to the ``AdapterError`` family so the poller and commands can
react uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import ClassVar


class Platform(StrEnum):
    HTB = "htb"
    THM = "thm"
    ROOTME = "rootme"


PLATFORM_LABELS: dict[Platform, str] = {
    Platform.HTB: "Hack The Box",
    Platform.THM: "TryHackMe",
    Platform.ROOTME: "Root-Me",
}


@dataclass(frozen=True)
class PlatformUser:
    """A canonicalized reference to an account on a platform."""

    platform: Platform
    user_id: str
    username: str
    #: Secondary identifiers some platforms need (THM has three id schemes).
    extra_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProfileStats:
    """One point-in-time reading of an account, feeding a snapshot row."""

    platform: Platform
    user_id: str
    username: str
    points: int
    #: Global ranking position (lower is better); None when the platform
    #: doesn't expose one or it couldn't be read.
    rank: int | None
    #: Platform-specific extra counters (e.g. HTB user_owns/system_owns).
    counters: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SolveEvent:
    """A normalized 'this account solved this thing' event."""

    platform: Platform
    item_ref: str
    name: str
    kind: str  # "user" | "root" | "challenge" | "room"
    points: int
    solved_at: datetime | None
    first_blood: bool = False


class AdapterError(Exception):
    """Base for all normalized platform failures."""


class ProfileNotFound(AdapterError):
    """The referenced account does not exist (or the platform hides it — see subclass)."""


class ProfilePrivate(ProfileNotFound):
    """The account exists but its profile is not public."""


class AuthExpired(AdapterError):
    """The bot-level platform credential is missing, invalid, or expired."""


class RateLimited(AdapterError):
    """The platform told us to slow down; retry later."""


class PlatformUnavailable(AdapterError):
    """The platform can't be reached usefully right now (outage, bot challenge, 5xx)."""


class PlatformAdapter(ABC):
    """Common interface every platform integration implements."""

    platform: ClassVar[Platform]
    #: Whether this platform supports bio-token ownership verification.
    supports_verification: ClassVar[bool] = False

    @abstractmethod
    async def resolve_user(self, user_ref: str) -> PlatformUser:
        """Validate a user-supplied id/username at /link time and canonicalize it.

        Raises ProfileNotFound/ProfilePrivate for bad refs so the user gets a
        clear error immediately rather than a silently broken link.
        """

    @abstractmethod
    async def get_profile(self, user: PlatformUser) -> ProfileStats:
        """Fetch current stats for a linked account."""

    @abstractmethod
    async def get_recent_solves(
        self, user: PlatformUser, *, deep: bool = False
    ) -> list[SolveEvent]:
        """Fetch solve events. Best-effort: platforms without a usable source
        return an empty list rather than raising.

        ``deep=True`` asks for the account's full history (paging as far as the
        platform allows). The poller sets it only on a link's FIRST poll, to
        backfill; later polls take the cheap recent-only path.
        """

    async def poll(
        self, user: PlatformUser, *, deep: bool = False
    ) -> tuple[ProfileStats, list[SolveEvent]]:
        """One poll cycle. Adapters where a single request serves both reads
        (Root-Me) override this to avoid a second round-trip."""
        stats = await self.get_profile(user)
        solves = await self.get_recent_solves(user, deep=deep)
        return stats, solves

    async def get_verification_token_haystack(self, user: PlatformUser) -> str | None:
        """Return public, user-editable profile text in which an ownership
        token can be found, or None when the platform exposes no such field.

        Not every platform has a "bio": HTB exposes only social-link fields
        (live-verified), Root-Me exposes nothing. Each adapter documents which
        field(s) it reads and the command tells the user where to paste it.
        """
        return None

    #: Where the user should paste their verification token, shown by /verify.
    verification_instructions: ClassVar[str] = ""
