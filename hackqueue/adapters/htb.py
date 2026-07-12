"""Hack The Box adapter (v4 API, single bot-level App Token).

Live-verified quirks this module encodes (ARCHITECTURE.md §4):
- ``Accept: application/json`` is mandatory — without it auth failures are a
  302 to an HTML login page instead of a JSON 401. Redirects are treated as
  auth failures and never followed.
- 404 on profile endpoints means "nonexistent or private" (HTB does not
  distinguish them for us); we surface it as ProfilePrivate with guidance.
- ``stars`` on machine objects is a string.
- Active and retired machines live on two separate paginated endpoints.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, ClassVar

from hackqueue.adapters.base import (
    AuthExpired,
    Platform,
    PlatformAdapter,
    PlatformUnavailable,
    PlatformUser,
    ProfilePrivate,
    ProfileStats,
    RateLimited,
    SolveEvent,
)
from hackqueue.http.client import HttpClient, HttpResult

# All HTB URLs live here: unofficial APIs drift, patch in one place.
BASE = "https://labs.hackthebox.com/api/v4"
URL_PROFILE_BASIC = BASE + "/user/profile/basic/{user_id}"
URL_PROFILE_ACTIVITY = BASE + "/user/profile/activity/{user_id}"
URL_MACHINES_ACTIVE = BASE + "/machine/paginated?per_page=100&page={page}"
URL_MACHINES_RETIRED = BASE + "/machine/list/retired/paginated?per_page=100&page={page}"
PROFILE_WEB_URL = "https://app.hackthebox.com/profile/{user_id}"
MACHINE_WEB_URL = "https://app.hackthebox.com/machines/{ref}"

_PROFILE_URL_RE = re.compile(r"(?:app\.hackthebox\.com/(?:profile|users)/)(\d+)", re.I)
# Candidate bio fields, best-first; which one the v4 profile actually exposes
# is pending confirmation with a real token (open item in ARCHITECTURE.md §6).
_BIO_FIELDS = ("description", "bio", "biography", "about")


class HTBAdapter(PlatformAdapter):
    platform: ClassVar[Platform] = Platform.HTB
    supports_verification: ClassVar[bool] = True

    def __init__(self, http: HttpClient, app_token: str) -> None:
        self._http = http
        self._headers = {
            "Authorization": f"Bearer {app_token}",
            "Accept": "application/json",
        }

    async def resolve_user(self, user_ref: str) -> PlatformUser:
        user_id = self._parse_ref(user_ref)
        profile = await self._fetch_profile(user_id)
        return PlatformUser(
            platform=Platform.HTB, user_id=user_id, username=str(profile.get("name", user_id))
        )

    async def get_profile(self, user: PlatformUser) -> ProfileStats:
        profile = await self._fetch_profile(user.user_id)
        return ProfileStats(
            platform=Platform.HTB,
            user_id=user.user_id,
            username=str(profile.get("name", user.username)),
            points=int(profile.get("points") or 0),
            rank=_int_or_none(profile.get("ranking")),
            counters={
                "user_owns": int(profile.get("user_owns") or 0),
                "system_owns": int(profile.get("system_owns") or 0),
                "respects": int(profile.get("respects") or 0),
            },
        )

    async def get_recent_solves(self, user: PlatformUser) -> list[SolveEvent]:
        result = await self._http.get(
            URL_PROFILE_ACTIVITY.format(user_id=user.user_id), headers=self._headers
        )
        self._raise_for_status(result)
        activity = (result.data or {}).get("profile", {}).get("activity", []) or []
        solves: list[SolveEvent] = []
        for entry in activity:
            object_type = entry.get("object_type")
            if object_type == "machine":
                kind = "root" if entry.get("type") == "root" else "user"
            elif object_type == "challenge":
                kind = "challenge"
            else:
                continue  # fortress/endgame etc. — not scored as solves
            solves.append(
                SolveEvent(
                    platform=Platform.HTB,
                    item_ref=str(entry.get("id", "")),
                    name=str(entry.get("name", "?")),
                    kind=kind,
                    points=int(entry.get("points") or 0),
                    solved_at=_parse_date(entry.get("date")),
                    first_blood=bool(entry.get("first_blood", False)),
                )
            )
        return solves

    async def get_verification_bio(self, user: PlatformUser) -> str | None:
        profile = await self._fetch_profile(user.user_id)
        for field in _BIO_FIELDS:
            if field in profile:
                return str(profile[field] or "")
        return None

    async def iter_machines(self) -> AsyncIterator[dict[str, Any]]:
        """Yield normalized machine dicts from BOTH catalogs (active + retired)."""
        for url_template, retired in ((URL_MACHINES_ACTIVE, False), (URL_MACHINES_RETIRED, True)):
            page, last_page = 1, 1
            while page <= last_page:
                result = await self._http.get(url_template.format(page=page), headers=self._headers)
                self._raise_for_status(result)
                body = result.data or {}
                last_page = int(body.get("meta", {}).get("last_page") or 1)
                for m in body.get("data", []) or []:
                    yield self._normalize_machine(m, retired=retired)
                page += 1

    @staticmethod
    def _normalize_machine(m: dict[str, Any], *, retired: bool) -> dict[str, Any]:
        return {
            "platform_ref": str(m.get("id", "")),
            "name": str(m.get("name", "?")),
            "os": (m.get("os") or None),
            "difficulty": (m.get("difficultyText") or "").lower() or None,
            "tags": [t.get("name", "") for t in m.get("tags", []) or [] if isinstance(t, dict)],
            "retired": retired,
            "stars": _float_or_none(m.get("stars")),  # string in the API
            "release_date": _parse_date(m.get("release")),
            "url": MACHINE_WEB_URL.format(ref=m.get("id", "")),
        }

    async def _fetch_profile(self, user_id: str) -> dict[str, Any]:
        result = await self._http.get(
            URL_PROFILE_BASIC.format(user_id=user_id), headers=self._headers
        )
        self._raise_for_status(result)
        profile = (result.data or {}).get("profile")
        if not isinstance(profile, dict):
            raise PlatformUnavailable("HTB returned an unexpected response shape")
        return profile

    @staticmethod
    def _parse_ref(user_ref: str) -> str:
        """Accept a raw numeric HTB id or a profile URL."""
        ref = user_ref.strip()
        if ref.isdigit():
            return ref
        if match := _PROFILE_URL_RE.search(ref):
            return match.group(1)
        raise ProfilePrivate(
            "That doesn't look like an HTB user ID. Use the number from your profile URL: "
            "https://app.hackthebox.com/profile/<id>"
        )

    @staticmethod
    def _raise_for_status(result: HttpResult) -> None:
        if 200 <= result.status < 300 and result.is_json:
            return
        if result.status in (301, 302, 401, 403):
            # 302 -> HTML login page is HTB's signature for missing/expired tokens
            raise AuthExpired(
                "HTB App Token is missing, invalid, or expired — regenerate it in your "
                "HTB profile settings"
            )
        if result.status == 404:
            raise ProfilePrivate(
                "HTB profile not found — check the ID, and make sure the profile is public "
                "(HTB profile settings → Public Profile)"
            )
        if result.status == 429:
            raise RateLimited("HTB rate limit hit")
        raise PlatformUnavailable(f"HTB API returned HTTP {result.status}")


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
