"""In-memory per-platform health, feeding /health and board staleness markers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from hackqueue.adapters.base import AdapterError, AuthExpired, Platform
from hackqueue.db.models import utcnow
from hackqueue.log import get_logger

log = get_logger(__name__)


class PlatformStatus(StrEnum):
    UNKNOWN = "unknown"
    OK = "ok"
    DEGRADED = "degraded"
    AUTH_ERROR = "auth_error"


@dataclass
class HealthEntry:
    status: PlatformStatus = PlatformStatus.UNKNOWN
    last_success: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None


@dataclass
class HealthRegistry:
    entries: dict[Platform, HealthEntry] = field(default_factory=dict)

    def entry(self, platform: Platform) -> HealthEntry:
        return self.entries.setdefault(platform, HealthEntry())

    def record_success(self, platform: Platform) -> None:
        e = self.entry(platform)
        if e.status is not PlatformStatus.OK:
            log.info("platform_recovered", platform=platform.value)
        e.status = PlatformStatus.OK
        e.last_success = utcnow()

    def record_error(self, platform: Platform, error: AdapterError) -> None:
        e = self.entry(platform)
        e.status = (
            PlatformStatus.AUTH_ERROR if isinstance(error, AuthExpired) else PlatformStatus.DEGRADED
        )
        e.last_error = str(error)
        e.last_error_at = utcnow()
        log.warning(
            "platform_unhealthy", platform=platform.value, status=e.status, error=str(error)
        )

    def is_stale(self, platform: Platform) -> bool:
        return self.entry(platform).status in (
            PlatformStatus.DEGRADED,
            PlatformStatus.AUTH_ERROR,
        )
