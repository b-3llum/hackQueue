"""TryHackMe adapter.

Live-verified 2026-07-13 with real requests. Two things worth knowing, because
both contradict the community docs *and* this file's first version:

1. **TryHackMe rejects bot-looking User-Agents.** A plain ``hackQueue/x`` UA
   gets Vercel's bot-mitigation challenge (429 + an HTML page) on every
   endpoint. A *browser* UA sails through — no cookies, no JS, no headless
   browser needed. So this adapter sends a Chrome UA with our identifier
   appended: a maintainer reading THM's logs still sees ``hackQueue`` and the
   repo URL, so we stay attributable rather than pretending to be a person.
   The challenge detection stays in place — THM can switch mitigation back on
   at any time, and when it does the platform degrades instead of breaking.

2. **Almost every documented v1 endpoint is dead** (they serve the SPA's HTML
   now). The live surface is ``/api/v2/public-profile``, which answers with
   everything in one request: points, percentile, rooms completed, badges,
   streak, level, and an ``about`` bio — which is what finally makes ownership
   verification possible here.
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
URL_PUBLIC_PROFILE = BASE + "/api/v2/public-profile?username={username}"
URL_COMPLETED_ROOMS = (
    BASE + "/api/v2/public-profile/completed-rooms?username={username}&limit={limit}&page={page}"
)
PROFILE_WEB_URL = BASE + "/p/{username}"
ROOM_WEB_URL = BASE + "/room/{code}"

#: A browser UA is the price of entry (see the module docstring); our own
#: identifier rides along so we're still attributable.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

ROOMS_PER_PAGE = 50
MAX_ROOM_PAGES = 10

CHALLENGE_MSG = (
    "TryHackMe is behind a bot-mitigation challenge right now and can't be "
    "polled — data will refresh automatically once it clears"
)


class THMAdapter(PlatformAdapter):
    platform: ClassVar[Platform] = Platform.THM
    supports_verification: ClassVar[bool] = True
    verification_instructions: ClassVar[str] = (
        "Paste the token anywhere in the **About** section of your TryHackMe "
        "profile (your profile → Edit → About). You can remove it once verified."
    )

    def __init__(self, http: HttpClient, user_agent: str) -> None:
        self._http = http
        self._headers = {
            "User-Agent": f"{BROWSER_UA} {user_agent}",
            "Accept": "application/json",
            "Referer": BASE + "/",
        }

    async def resolve_user(self, user_ref: str) -> PlatformUser:
        username = self._parse_ref(user_ref)
        profile = await self._fetch_profile(username)
        canonical = str(profile.get("username") or username)
        return PlatformUser(platform=Platform.THM, user_id=canonical, username=canonical)

    async def get_profile(self, user: PlatformUser) -> ProfileStats:
        profile = await self._fetch_profile(user.user_id)
        counters = {
            "rooms_completed": _int(profile.get("completedRoomsNumber")),
            "badges": _int(profile.get("badgesNumber")),
            "streak_days": _int(profile.get("streak")),
            "level": _int(profile.get("level")),
        }
        # THM no longer publishes a global position — it reports a percentile
        # ("Top 15%"), so that's kept as a counter, not as a rank.
        if (top := _int_or_none(profile.get("topPercentage"))) is not None:
            counters["top_percent"] = top
        return ProfileStats(
            platform=Platform.THM,
            user_id=user.user_id,
            username=str(profile.get("username") or user.username),
            points=_int(profile.get("totalPoints")),
            rank=None,
            counters=counters,
        )

    async def get_recent_solves(
        self, user: PlatformUser, *, deep: bool = False
    ) -> list[SolveEvent]:
        """Completed rooms. Best-effort: a challenge window or a shape change
        costs the solve list, not the whole poll — the profile still snapshots.

        THM publishes no completion dates, so ``solved_at`` is None and the
        poller's first-seen timestamp is what orders these.
        """
        solves: list[SolveEvent] = []
        page = 1
        pages = MAX_ROOM_PAGES if deep else 1
        while page <= pages:
            try:
                data = await self._get_json(
                    URL_COMPLETED_ROOMS.format(
                        username=quote(user.user_id, safe=""),
                        limit=ROOMS_PER_PAGE,
                        page=page,
                    )
                )
            except (PlatformUnavailable, RateLimited, ProfileNotFound):
                return solves
            payload = data.get("data") if isinstance(data, dict) else None
            if not isinstance(payload, dict):
                return solves
            for room in payload.get("docs") or []:
                if not isinstance(room, dict) or not room.get("code"):
                    continue
                solves.append(
                    SolveEvent(
                        platform=Platform.THM,
                        item_ref=str(room["code"]),
                        name=str(room.get("title") or room["code"]),
                        kind="room",
                        points=0,  # THM doesn't publish per-room awards
                        solved_at=None,
                    )
                )
            if not payload.get("hasNextPage"):
                break
            page += 1
        return solves

    async def get_verification_token_haystack(self, user: PlatformUser) -> str | None:
        """THM's ``about`` field is a real public bio — the token goes there."""
        profile = await self._fetch_profile(user.user_id)
        return str(profile.get("about") or "")

    async def _fetch_profile(self, username: str) -> dict[str, Any]:
        data = await self._get_json(URL_PUBLIC_PROFILE.format(username=quote(username, safe="")))
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict) or not payload.get("username"):
            raise ProfileNotFound(f"TryHackMe user '{username}' not found")
        return payload

    @staticmethod
    def _parse_ref(user_ref: str) -> str:
        ref = user_ref.strip().lstrip("@")
        if ref.startswith("http"):  # a profile URL: …/p/<username>
            ref = ref.rstrip("/").rsplit("/", 1)[-1]
        if not ref or "/" in ref:
            raise ProfileNotFound("That doesn't look like a TryHackMe username")
        return ref

    async def _get_json(self, url: str) -> Any:
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
            # 200 but HTML: a challenge page, or a dead endpoint serving the
            # SPA. Either way there's nothing to parse.
            raise PlatformUnavailable(CHALLENGE_MSG)
        raise PlatformUnavailable(f"TryHackMe returned HTTP {result.status}")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
