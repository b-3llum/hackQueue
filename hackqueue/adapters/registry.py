"""Adapter registry: which platforms are enabled for this deployment.

A platform whose credential is missing simply isn't registered — commands
mention it as disabled, the poller skips it, nothing crashes.
"""

from __future__ import annotations

from collections.abc import Iterator

from hackqueue import USER_AGENT
from hackqueue.adapters.base import Platform, PlatformAdapter
from hackqueue.adapters.htb import HTBAdapter
from hackqueue.adapters.rootme import RootMeAdapter
from hackqueue.adapters.thm import THMAdapter
from hackqueue.config import Settings
from hackqueue.http.client import HttpClient
from hackqueue.log import get_logger

log = get_logger(__name__)

# Per-host minimum spacing between requests, from live-verified behavior:
# HTB 429s under burst load; Root-Me throttles aggressively per IP.
HOST_MIN_INTERVALS = {
    "labs.hackthebox.com": 1.0,
    "api.www.root-me.org": 1.5,
    "tryhackme.com": 1.0,
}


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[Platform, PlatformAdapter] = {}

    def register(self, adapter: PlatformAdapter) -> None:
        self._adapters[adapter.platform] = adapter

    def get(self, platform: Platform | str) -> PlatformAdapter | None:
        return self._adapters.get(Platform(platform))

    @property
    def platforms(self) -> list[Platform]:
        return list(self._adapters)

    def __iter__(self) -> Iterator[PlatformAdapter]:
        return iter(self._adapters.values())

    def __contains__(self, platform: Platform | str) -> bool:
        return Platform(platform) in self._adapters


def build_http_client(user_agent: str = USER_AGENT) -> HttpClient:
    return HttpClient(user_agent=user_agent, min_intervals=HOST_MIN_INTERVALS)


def build_registry(http: HttpClient, settings: Settings) -> AdapterRegistry:
    registry = AdapterRegistry()
    if settings.htb_app_token:
        registry.register(HTBAdapter(http, settings.htb_app_token))
    else:
        log.warning("adapter_disabled", platform="htb", reason="HTB_APP_TOKEN not set")
    if settings.rootme_api_key:
        registry.register(RootMeAdapter(http, settings.rootme_api_key))
    else:
        log.warning("adapter_disabled", platform="rootme", reason="ROOTME_API_KEY not set")
    registry.register(THMAdapter(http, USER_AGENT))  # no credential needed
    return registry
