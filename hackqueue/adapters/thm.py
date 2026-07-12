"""TryHackMe adapter (unofficial endpoints — best-effort by design).

TryHackMe sits behind Vercel's bot mitigation, which (as live-verified on
2026-07-12, ARCHITECTURE.md §4) answers plain-HTTP API calls with a
429 text/html JS challenge. This adapter therefore:

- treats a challenge response as ``PlatformUnavailable`` (the poller flips
  THM to "degraded" and boards render the last snapshots with a staleness
  marker — it must never look like a rate limit or an empty profile);
- validates response shapes before trusting them (the documented shapes are
  community knowledge and could not be live-verified);
- captures every THM identifier scheme it can at link time (username for v1
  endpoints, userPublicId / user hash for v2) into ``extra_ids``.

None of the shapes here are guaranteed; that is why THM data is best-effort
and why every URL lives in the constants block below.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import quote

from hackqueue.adapters.base import (
    Platform,
    PlatformAdapter,
    PlatformUnavailable,
    PlatformUser,
    ProfileNotFound,
    ProfileStats,
    RateLimited,
    SolveEvent,
)
from hackqueue.http.client import HttpClient, HttpResult
from hackqueue.log import get_logger

log = get_logger(__name__)

BASE = "https://tryhackme.com"
# v1 (username-keyed). /api/discord/user was purpose-built for Discord bots:
# one call returns rank + points + subscription flag.
URL_DISCORD_USER = BASE + "/api/discord/user/{username}"
URL_RANK = BASE + "/api/user/rank/{username}"
URL_COMPLETED_COUNT = BASE + "/api/no-completed-rooms-public/{username}"
# v2 (id-keyed)
URL_PUBLIC_PROFILE = BASE + "/api/v2/public-profile?username={username}"
URL_COMPLETED_ROOMS = (
    BASE + "/api/v2/public-profile/completed-rooms?user={user_hash}&limit=25&page=1"
)
PROFILE_WEB_URL = "https://tryhackme.com/p/{username}"

CHALLENGE_MSG = (
    "TryHackMe is currently behind a bot-mitigation challenge and can't be "
    "polled — data will refresh automatically once it clears"
)


class THMAdapter(PlatformAdapter):
    platform: ClassVar[Platform] = Platform.THM
    supports_verification: ClassVar[bool] = False  # deferred until API access is stable

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._headers = {"Accept": "application/json"}

    async def resolve_user(self, user_ref: str) -> PlatformUser:
        username = user_ref.strip().lstrip("@")
        if not username or "/" in username:
            raise ProfileNotFound("That doesn't look like a TryHackMe username")
        data = await self._get_checked(URL_DISCORD_USER.format(username=_enc(username)))
        if not isinstance(data, dict) or "points" not in data:
            raise ProfileNotFound(f"TryHackMe user '{username}' not found")
        extra_ids = await self._try_capture_v2_ids(username)
        return PlatformUser(
            platform=Platform.THM, user_id=username, username=username, extra_ids=extra_ids
        )

    async def get_profile(self, user: PlatformUser) -> ProfileStats:
        data = await self._get_checked(URL_DISCORD_USER.format(username=_enc(user.user_id)))
        if not isinstance(data, dict) or "points" not in data:
            raise ProfileNotFound(f"TryHackMe user '{user.user_id}' not found")
        counters: dict[str, int] = {}
        if (sub := data.get("subscribed")) is not None:
            counters["subscribed"] = int(bool(sub))
        rooms = await self._try_completed_count(user.user_id)
        if rooms is not None:
            counters["rooms_completed"] = rooms
        return ProfileStats(
            platform=Platform.THM,
            user_id=user.user_id,
            username=user.username,
            points=int(data.get("points") or 0),
            rank=_int_or_none(data.get("userRank")),
            counters=counters,
        )

    async def get_recent_solves(
        self, user: PlatformUser, *, deep: bool = False
    ) -> list[SolveEvent]:
        """Recent completed rooms via the v2 endpoint. Best-effort: any failure
        (challenge window, missing user hash, shape drift) returns []."""
        user_hash = user.extra_ids.get("user_hash")
        if not user_hash:
            return []
        try:
            data = await self._get_checked(URL_COMPLETED_ROOMS.format(user_hash=_enc(user_hash)))
        except (PlatformUnavailable, RateLimited, ProfileNotFound):
            return []
        rooms = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(rooms, list):
            return []
        solves = []
        for room in rooms:
            if not isinstance(room, dict):
                continue
            code = room.get("code") or room.get("roomCode")
            if not code:
                continue
            solves.append(
                SolveEvent(
                    platform=Platform.THM,
                    item_ref=str(code),
                    name=str(room.get("title") or code),
                    kind="room",
                    points=0,  # THM doesn't expose per-room point awards publicly
                    solved_at=None,
                )
            )
        return solves

    async def _try_capture_v2_ids(self, username: str) -> dict[str, str]:
        """Best-effort: v2 endpoints key on ids, not usernames — grab them now
        so solves can be fetched later even if this endpoint drifts."""
        try:
            data = await self._get_checked(URL_PUBLIC_PROFILE.format(username=_enc(username)))
        except (PlatformUnavailable, RateLimited, ProfileNotFound):
            return {}
        if not isinstance(data, dict):
            return {}
        payload = data.get("data", data)
        if not isinstance(payload, dict):
            return {}
        extra: dict[str, str] = {}
        for ours, theirs in (
            ("user_public_id", "userPublicId"),
            ("user_hash", "userId"),
            ("user_hash", "_id"),
        ):
            value = payload.get(theirs)
            if value and ours not in extra:
                extra[ours] = str(value)
        return extra

    async def _try_completed_count(self, username: str) -> int | None:
        try:
            data = await self._get_checked(URL_COMPLETED_COUNT.format(username=_enc(username)))
        except (PlatformUnavailable, RateLimited, ProfileNotFound):
            return None
        return _int_or_none(data)

    async def _get_checked(self, url: str) -> Any:
        result = await self._http.get(url, headers=self._headers)
        self._raise_for_status(result)
        return result.data

    @staticmethod
    def _raise_for_status(result: HttpResult) -> None:
        if result.is_challenge:
            raise PlatformUnavailable(CHALLENGE_MSG)
        if 200 <= result.status < 300 and result.is_json:
            return
        if result.status == 404:
            raise ProfileNotFound("TryHackMe user not found")
        if result.status == 429:
            raise RateLimited("TryHackMe rate limit hit")
        if 200 <= result.status < 300:
            # 200 but not JSON => challenge page or shape drift; treat as outage
            raise PlatformUnavailable(CHALLENGE_MSG)
        raise PlatformUnavailable(f"TryHackMe returned HTTP {result.status}")


def _enc(value: str) -> str:
    """User-supplied identifiers must never reshape the URL (query/path injection)."""
    return quote(value, safe="")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
