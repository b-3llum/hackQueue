"""Hack The Box adapter (single bot-level App Token).

Everything here was verified against the live API with a real token on
2026-07-12 — the community Postman docs are stale in several places and
following them yields silently-empty results:

- The activity feed lives on **v5**, not v4 (``/api/v4/user/profile/activity/{id}``
  is a hard 404 today). It is paginated (``data`` + ``meta.lastPage``) and its
  fields are ``type`` / ``ownDate`` / ``blood`` — NOT ``object_type`` / ``date``
  / ``first_blood`` as documented.
- ``Accept: application/json`` is mandatory: without it, auth failures answer
  with a 302 to an HTML login page instead of a JSON 401.
- An invalid user id returns **400** (``{"message":{"user_id":[…]}}``), not 404.
- Profiles carry a ``public`` boolean — that, not an error status, is how a
  private profile presents.
- There is **no bio/description field** on HTB profiles. Ownership
  verification therefore reads the user-editable social-link fields
  (github/linkedin/twitter/cv) instead; see ``get_verification_token_haystack``.
- Machine catalogs: active and retired are separate paginated endpoints;
  ``stars`` may arrive as a string.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, ClassVar

from hackqueue.adapters.base import (
    AdapterError,
    AuthExpired,
    Platform,
    PlatformAdapter,
    PlatformUnavailable,
    PlatformUser,
    ProfileNotFound,
    ProfilePrivate,
    ProfileStats,
    RateLimited,
    SolveEvent,
)
from hackqueue.http.client import HttpClient, HttpResult

# All HTB URLs live here: this API drifts, so it gets patched in one place.
BASE_V4 = "https://labs.hackthebox.com/api/v4"
BASE_V5 = "https://labs.hackthebox.com/api/v5"
URL_PROFILE_BASIC = BASE_V4 + "/user/profile/basic/{user_id}"
URL_PROFILE_ACTIVITY = BASE_V5 + "/user/profile/activity/{user_id}?page={page}"
URL_MACHINES_ACTIVE = BASE_V4 + "/machine/paginated?per_page=100&page={page}"
URL_MACHINES_RETIRED = BASE_V4 + "/machine/list/retired/paginated?per_page=100&page={page}"
URL_PROGRESS_PROLAB = BASE_V4 + "/user/profile/progress/prolab/{user_id}"
URL_PROGRESS_FORTRESS = BASE_V4 + "/user/profile/progress/fortress/{user_id}"
URL_PROGRESS_CHALLENGES = BASE_V4 + "/user/profile/progress/challenges/{user_id}"
URL_SEASON_LIST = BASE_V4 + "/season/list"
URL_SEASON_MACHINE_ACTIVE = BASE_V4 + "/season/machine/active"
URL_SEASON_MACHINES = BASE_V4 + "/season/machines/{season_id}"
MACHINE_WEB_URL = "https://app.hackthebox.com/machines/{ref}"

_PROFILE_URL_RE = re.compile(r"(?:app\.hackthebox\.com/(?:profile|users)/)(\d+)", re.I)
#: Public, user-editable profile fields — HTB has no bio, so a verification
#: token is pasted into one of these (a URL like https://x.com/hq-ab12cd34
#: works even if HTB validates the field as a URL).
_SOCIAL_FIELDS = ("twitter", "github", "linkedin", "cv")
#: Machine owns and challenge solves are separate id spaces on HTB.
_MACHINE_KINDS = frozenset({"user", "root"})
#: Pages of activity (15 entries each) to walk when backfilling a new link.
MAX_ACTIVITY_PAGES = 20


class HTBAdapter(PlatformAdapter):
    platform: ClassVar[Platform] = Platform.HTB
    supports_verification: ClassVar[bool] = True
    verification_instructions: ClassVar[str] = (
        "HTB has no bio field, so paste the token into any **social link** in "
        "HTB → Profile Settings (Twitter/X, GitHub, LinkedIn or CV). A URL "
        "containing it works too, e.g. `https://x.com/{token}`. "
        "You can restore your real link once verified."
    )

    def __init__(self, http: HttpClient, app_token: str) -> None:
        self._http = http
        self._headers = {
            "Authorization": f"Bearer {app_token}",
            "Accept": "application/json",  # or auth failures 302 to an HTML page
        }

    async def resolve_user(self, user_ref: str) -> PlatformUser:
        user_id = self._parse_ref(user_ref)
        profile = await self._fetch_profile(user_id)
        return PlatformUser(
            platform=Platform.HTB, user_id=user_id, username=str(profile.get("name", user_id))
        )

    async def get_profile(self, user: PlatformUser) -> ProfileStats:
        """Score HTB accounts on **flags captured**, not HTB's own rank points.

        HTB strips a machine's points the moment it retires, so a member who
        has owned nothing but retired boxes reads as ``points: 0`` — which
        makes a points-based board useless for exactly the people doing the
        most learning (live-verified: an account with 4 user + 4 root owns and
        a completed Pro Lab reports 0 points).

        Flags are what HTB actually counts everywhere: a machine's user and
        root own are one flag each, a solved challenge is one, and Pro
        Labs/Fortresses report ``owned_flags`` directly. No invented weights,
        and progress on retired content and Pro Labs finally shows up.
        """
        profile = await self._fetch_profile(user.user_id)
        machine_flags = int(profile.get("user_owns") or 0) + int(profile.get("system_owns") or 0)
        # Each of these is best-effort: a missing section costs its flags, not
        # the whole poll.
        challenge_flags = await self._challenge_flags(user.user_id)
        prolab_flags, prolabs_done = await self._lab_flags(
            URL_PROGRESS_PROLAB.format(user_id=user.user_id), "prolabs"
        )
        fortress_flags, _ = await self._lab_flags(
            URL_PROGRESS_FORTRESS.format(user_id=user.user_id), "fortresses"
        )
        return ProfileStats(
            platform=Platform.HTB,
            user_id=user.user_id,
            username=str(profile.get("name", user.username)),
            points=machine_flags + challenge_flags + prolab_flags + fortress_flags,
            rank=_int_or_none(profile.get("ranking")),
            counters={
                "user_owns": int(profile.get("user_owns") or 0),
                "system_owns": int(profile.get("system_owns") or 0),
                "challenges": challenge_flags,
                "prolab_flags": prolab_flags,
                "prolabs_completed": prolabs_done,
                "fortress_flags": fortress_flags,
                "user_bloods": int(profile.get("user_bloods") or 0),
                "system_bloods": int(profile.get("system_bloods") or 0),
                "respects": int(profile.get("respects") or 0),
                # HTB's own rank points, kept for reference — usually 0 for
                # anyone whose owns have retired.
                "htb_rank_points": int(profile.get("points") or 0),
            },
        )

    async def _challenge_flags(self, user_id: str) -> int:
        try:
            result = await self._http.get(
                URL_PROGRESS_CHALLENGES.format(user_id=user_id), headers=self._headers
            )
            self._raise_for_status(result)
        except AdapterError:
            return 0
        owns = (result.data or {}).get("profile", {}).get("challenge_owns", {})
        return int(owns.get("solved") or 0)

    async def _lab_flags(self, url: str, key: str) -> tuple[int, int]:
        """(flags owned, labs at 100%) across Pro Labs or Fortresses."""
        try:
            result = await self._http.get(url, headers=self._headers)
            self._raise_for_status(result)
        except AdapterError:
            return 0, 0
        labs = (result.data or {}).get("profile", {}).get(key) or []
        flags = sum(int(lab.get("owned_flags") or 0) for lab in labs if isinstance(lab, dict))
        completed = sum(
            1
            for lab in labs
            if isinstance(lab, dict) and int(lab.get("completion_percentage") or 0) >= 100
        )
        return flags, completed

    async def get_recent_solves(
        self, user: PlatformUser, *, deep: bool = False
    ) -> list[SolveEvent]:
        """Page 1 is the recent feed; ``deep`` walks the whole history so a new
        link backfills its owns (used to exclude solved boxes from /suggest)."""
        solves: list[SolveEvent] = []
        page, last_page = 1, 1
        while page <= last_page and page <= MAX_ACTIVITY_PAGES:
            result = await self._http.get(
                URL_PROFILE_ACTIVITY.format(user_id=user.user_id, page=page),
                headers=self._headers,
            )
            self._raise_for_status(result)
            body = result.data if isinstance(result.data, dict) else {}
            entries = body.get("data")
            if not isinstance(entries, list):
                raise PlatformUnavailable("HTB activity returned an unexpected shape")
            for entry in entries:
                if solve := self._to_solve(entry):
                    solves.append(solve)
            if not deep:
                break
            last_page = int(body.get("meta", {}).get("lastPage") or 1)
            page += 1
        return solves

    @staticmethod
    def _to_solve(entry: Any) -> SolveEvent | None:
        if not isinstance(entry, dict):
            return None
        kind = str(entry.get("type") or "")
        if kind not in _MACHINE_KINDS and kind != "challenge":
            return None  # fortress/endgame/prolab — not scored as solves
        return SolveEvent(
            platform=Platform.HTB,
            item_ref=str(entry.get("id", "")),
            name=str(entry.get("name", "?")),
            kind=kind,
            points=int(entry.get("points") or 0),
            solved_at=_parse_date(entry.get("ownDate")),
            first_blood=bool(entry.get("blood", False)),
        )

    async def get_verification_token_haystack(self, user: PlatformUser) -> str | None:
        profile = await self._fetch_profile(user.user_id)
        return " ".join(str(profile.get(field) or "") for field in _SOCIAL_FIELDS)

    async def iter_machines(self) -> AsyncIterator[dict[str, Any]]:
        """Yield normalized machines from BOTH catalogs (active + retired)."""
        for url_template, retired in ((URL_MACHINES_ACTIVE, False), (URL_MACHINES_RETIRED, True)):
            page, last_page = 1, 1
            while page <= last_page:
                result = await self._http.get(url_template.format(page=page), headers=self._headers)
                self._raise_for_status(result)
                body = result.data if isinstance(result.data, dict) else {}
                last_page = int(body.get("meta", {}).get("last_page") or 1)
                for machine in body.get("data") or []:
                    if isinstance(machine, dict):
                        yield self._normalize_machine(machine, retired=retired)
                page += 1

    @staticmethod
    def _normalize_machine(m: dict[str, Any], *, retired: bool) -> dict[str, Any]:
        # Rating key differs by endpoint: the list sends numeric "star", the
        # detail endpoint sends "stars" (sometimes as a string).
        rating = m.get("star")
        if rating is None:
            rating = m.get("stars")
        return {
            "platform_ref": str(m.get("id", "")),
            "name": str(m.get("name", "?")),
            "os": (m.get("os") or None),
            "difficulty": (m.get("difficultyText") or "").lower() or None,
            # HTB dropped topic tags from the API; "labels" (SEASONAL, NEW, …)
            # is the only tag-like data left, so that's what /suggest filters on.
            "tags": [
                str(label["name"])
                for label in m.get("labels") or []
                if isinstance(label, dict) and label.get("name")
            ],
            "retired": retired,
            "stars": _float_or_none(rating),
            "release_date": _parse_date(m.get("release")),
            "url": MACHINE_WEB_URL.format(ref=m.get("id", "")),
        }

    # ── HTB Seasons ──────────────────────────────────────────────────────

    async def active_season(self) -> dict[str, Any] | None:
        """The season currently running, or None between seasons. Includes
        ``current_week``/``weeks`` so callers can show 'week 8 of 13'."""
        result = await self._http.get(URL_SEASON_LIST, headers=self._headers)
        self._raise_for_status(result)
        seasons = (result.data or {}).get("data") or []
        for season in seasons:
            if isinstance(season, dict) and (
                season.get("state") == "active" or season.get("active")
            ):
                return season
        return None

    async def active_season_machine(self) -> dict[str, Any] | None:
        """The machine dropped this week for the active season (None off-season)."""
        result = await self._http.get(URL_SEASON_MACHINE_ACTIVE, headers=self._headers)
        if result.status == 404:
            return None
        self._raise_for_status(result)
        machine = (result.data or {}).get("data")
        return machine if isinstance(machine, dict) else None

    async def season_machines(self, season_id: int) -> list[dict[str, Any]]:
        """The full weekly-drop schedule for a season (released + upcoming)."""
        result = await self._http.get(
            URL_SEASON_MACHINES.format(season_id=season_id), headers=self._headers
        )
        self._raise_for_status(result)
        machines = (result.data or {}).get("data") or []
        return [m for m in machines if isinstance(m, dict)]

    async def _fetch_profile(self, user_id: str) -> dict[str, Any]:
        result = await self._http.get(
            URL_PROFILE_BASIC.format(user_id=user_id), headers=self._headers
        )
        self._raise_for_status(result)
        profile = (result.data or {}).get("profile")
        if not isinstance(profile, dict):
            raise PlatformUnavailable("HTB returned an unexpected response shape")
        # Private profiles come back 200 with public=false, not as an error.
        if profile.get("public") is False:
            raise ProfilePrivate(
                "That HTB profile is private — turn on **Public Profile** in "
                "HTB → Profile Settings so the bot can read your stats."
            )
        return profile

    @staticmethod
    def _parse_ref(user_ref: str) -> str:
        """Accept a raw numeric HTB id or a profile URL."""
        ref = user_ref.strip()
        if ref.isdigit():
            return ref
        if match := _PROFILE_URL_RE.search(ref):
            return match.group(1)
        raise ProfileNotFound(
            "That doesn't look like an HTB user ID. Use the number from your profile "
            "URL: https://app.hackthebox.com/profile/**<id>**"
        )

    @staticmethod
    def _raise_for_status(result: HttpResult) -> None:
        if 200 <= result.status < 300 and result.is_json:
            return
        if result.status in (301, 302, 401, 403):
            # A 302 to the login page is HTB's signature for a bad/expired token.
            raise AuthExpired(
                "The HTB App Token is missing, invalid, or expired — the bot operator "
                "needs to regenerate it in HTB profile settings."
            )
        if result.status in (400, 404):
            raise ProfileNotFound("No HTB user with that ID — double-check the number.")
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
