"""Shared HTTP layer: one aiohttp session, identifiable UA, per-host request
spacing, and exponential backoff with jitter.

Behaviors here encode live-verified platform quirks (see ARCHITECTURE.md §4):

- Redirects are NEVER followed. Hack The Box answers auth failures with a
  302 to an HTML login page when ``Accept: application/json`` is missing, and
  even with it a redirect always means "not the JSON we wanted".
- A 429 carrying an ``x-vercel-mitigated`` header (TryHackMe's Vercel bot
  challenge) or an HTML body is NOT a rate limit: plain-HTTP retries can never
  succeed, so it is returned to the caller immediately instead of retried.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from hackqueue.log import get_logger

log = get_logger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
CHALLENGE_HEADER = "x-vercel-mitigated"


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    data: Any | None  # parsed body when the response was JSON
    text: str

    @property
    def is_json(self) -> bool:
        return self.data is not None

    @property
    def is_challenge(self) -> bool:
        """A bot-mitigation challenge page rather than a real API response."""
        return CHALLENGE_HEADER in self.headers or (
            self.status == 429 and "text/html" in self.headers.get("content-type", "")
        )


class _HostSpacing:
    """Serializes requests per host with a minimum interval between them."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_at: dict[str, float] = {}

    async def wait(self, host: str, min_interval: float) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            delay = self._next_at.get(host, 0.0) - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at[host] = max(now, self._next_at.get(host, 0.0)) + min_interval


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        min_intervals: dict[str, float] | None = None,
        timeout_seconds: float = 20.0,
        base_backoff: float = 1.0,
    ) -> None:
        self._user_agent = user_agent
        #: host -> minimum seconds between requests (e.g. Root-Me's per-IP throttle)
        self._min_intervals = min_intervals or {}
        self._base_backoff = base_backoff
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._spacing = _HostSpacing()
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        max_retries: int = 3,
        base_backoff: float | None = None,
    ) -> HttpResult:
        """GET with per-host spacing and backoff. Returns the final response
        whatever its status; callers map statuses to domain errors. Raises
        ``aiohttp.ClientError`` / ``asyncio.TimeoutError`` only when the
        network itself fails on every attempt."""
        if self._session is None:
            raise RuntimeError("HttpClient.start() was not called")
        host = urlsplit(url).netloc
        req_headers = {"User-Agent": self._user_agent, **(headers or {})}

        result: HttpResult | None = None
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            await self._spacing.wait(host, self._min_intervals.get(host, 0.0))
            try:
                async with self._session.get(
                    url, headers=req_headers, cookies=cookies, allow_redirects=False
                ) as resp:
                    text = await resp.text()
                    result = HttpResult(
                        status=resp.status,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                        data=_maybe_json(resp.headers.get("Content-Type", ""), text),
                        text=text,
                    )
            except (TimeoutError, aiohttp.ClientError) as exc:
                last_exc = exc
                result = None

            if result is not None:
                if result.is_challenge:
                    return result  # retrying a bot challenge over plain HTTP is futile
                if result.status not in RETRYABLE_STATUSES:
                    return result
            if attempt < max_retries:
                backoff = (
                    (base_backoff if base_backoff is not None else self._base_backoff)
                    * (2**attempt)
                    * random.uniform(0.7, 1.3)
                )
                log.debug(
                    "http_retry",
                    url=url,
                    attempt=attempt + 1,
                    status=result.status if result else repr(last_exc),
                    backoff=round(backoff, 2),
                )
                await asyncio.sleep(backoff)

        if result is None:
            assert last_exc is not None
            raise last_exc
        return result


def _maybe_json(content_type: str, text: str) -> Any | None:
    if "json" not in content_type:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None
