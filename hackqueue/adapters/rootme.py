"""Root-Me adapter (official API, api_key cookie on every request).

Live-verified quirks this module encodes (ARCHITECTURE.md §4):
- Auth is a COOKIE (``api_key``), not a header. Missing/invalid key → 401
  whose body is an error object wrapped in a JSON *array*.
- Success bodies may also be array-wrapped, list endpoints use numeric-string
  keys, and numeric fields ("score", "position", ids) arrive as strings —
  everything is normalized at this edge and nowhere else.
- Per-IP 429 throttling with no documented quota: the shared HttpClient
  enforces ~1 req/s spacing for this host (see bot wiring).
- One /auteurs/{id} response carries both stats and the solved-challenges
  list, so ``poll()`` is overridden to hit the API once per member per cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from hackqueue.adapters.base import (
    AuthExpired,
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

BASE = "https://api.www.root-me.org"  # the "www" is part of the hostname
URL_AUTHOR = BASE + "/auteurs/{author_id}"
URL_AUTHOR_SEARCH = BASE + "/auteurs?nom={name}"
PROFILE_WEB_URL = "https://www.root-me.org/?page=info_membre&id_auteur={author_id}"


class RootMeAdapter(PlatformAdapter):
    platform: ClassVar[Platform] = Platform.ROOTME
    supports_verification: ClassVar[bool] = False  # no bio via API; site is JS-challenged

    def __init__(self, http: HttpClient, api_key: str) -> None:
        self._http = http
        self._cookies = {"api_key": api_key}

    async def resolve_user(self, user_ref: str) -> PlatformUser:
        ref = user_ref.strip()
        if not ref.isdigit():
            raise ProfileNotFound(
                "Root-Me links use your numeric author ID. Find it with "
                "`/rootme-search <name>` or in your profile URL "
                "(…id_auteur=<number>)."
            )
        author = await self._fetch_author(ref)
        return PlatformUser(
            platform=Platform.ROOTME, user_id=ref, username=str(author.get("nom", ref))
        )

    async def get_profile(self, user: PlatformUser) -> ProfileStats:
        author = await self._fetch_author(user.user_id)
        return self._stats_from(author, user)

    async def get_recent_solves(self, user: PlatformUser) -> list[SolveEvent]:
        author = await self._fetch_author(user.user_id)
        return self._solves_from(author)

    async def poll(self, user: PlatformUser) -> tuple[ProfileStats, list[SolveEvent]]:
        author = await self._fetch_author(user.user_id)
        return self._stats_from(author, user), self._solves_from(author)

    async def search_by_name(self, name: str) -> list[tuple[str, str]]:
        """Search authors by name → [(author_id, nom)], to help users find their ID."""
        result = await self._http.get(URL_AUTHOR_SEARCH.format(name=name), cookies=self._cookies)
        self._raise_for_status(result)
        entries = _numeric_key_items(_unwrap(result.data))
        return [
            (str(e.get("id_auteur", "")), str(e.get("nom", "?")))
            for e in entries
            if isinstance(e, dict)
        ]

    def _stats_from(self, author: dict[str, Any], user: PlatformUser) -> ProfileStats:
        validations = _validations(author)
        return ProfileStats(
            platform=Platform.ROOTME,
            user_id=user.user_id,
            username=str(author.get("nom", user.username)),
            points=_as_int(author.get("score")),
            rank=_as_int(author.get("position")) or None,
            counters={"validations": len(validations)},
        )

    def _solves_from(self, author: dict[str, Any]) -> list[SolveEvent]:
        solves = []
        for entry in _validations(author):
            if not isinstance(entry, dict) or "id_challenge" not in entry:
                continue
            solves.append(
                SolveEvent(
                    platform=Platform.ROOTME,
                    item_ref=str(entry["id_challenge"]),
                    name=str(entry.get("titre") or f"Challenge {entry['id_challenge']}"),
                    kind="challenge",
                    points=0,  # per-challenge points aren't in the response; deltas come from score
                    solved_at=_parse_date(entry.get("date")),
                )
            )
        return solves

    async def _fetch_author(self, author_id: str) -> dict[str, Any]:
        result = await self._http.get(URL_AUTHOR.format(author_id=author_id), cookies=self._cookies)
        self._raise_for_status(result)
        author = _unwrap(result.data)
        if not isinstance(author, dict):
            raise PlatformUnavailable("Root-Me returned an unexpected response shape")
        return author

    @staticmethod
    def _raise_for_status(result: HttpResult) -> None:
        if 200 <= result.status < 300 and result.is_json:
            return
        if result.status == 401:
            raise AuthExpired(
                "Root-Me API key is missing or invalid — get one at "
                "https://www.root-me.org/?page=preferences"
            )
        if result.status == 404:
            raise ProfileNotFound("Root-Me author ID not found — check the number")
        if result.status == 429:
            raise RateLimited("Root-Me per-IP rate limit hit")
        raise PlatformUnavailable(f"Root-Me API returned HTTP {result.status}")


def _unwrap(data: Any) -> Any:
    """Root-Me wraps payloads in a single-element (or [payload, meta]) array."""
    if isinstance(data, list):
        return data[0] if data else {}
    return data


def _numeric_key_items(obj: Any) -> list[Any]:
    """List endpoints return {"0": {...}, "1": {...}} instead of a JSON array."""
    if isinstance(obj, dict):
        return [v for k, v in sorted(obj.items(), key=_numeric_sort_key) if k.isdigit()]
    if isinstance(obj, list):
        return obj
    return []


def _numeric_sort_key(item: tuple[str, Any]) -> int:
    return int(item[0]) if item[0].isdigit() else 10**9


def _validations(author: dict[str, Any]) -> list[Any]:
    raw = author.get("validations")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return _numeric_key_items(raw)
    return []


def _as_int(value: Any) -> int:
    """Root-Me sends numbers as strings; be liberal."""
    try:
        return int(str(value)) if value not in (None, "") else 0
    except ValueError:
        return 0


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None
