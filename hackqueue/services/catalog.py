"""Local box catalog: HTB machines (active + retired) enriched with IppSec
walkthrough links from the dataset behind ippsec.rocks.

Dataset facts (live-verified, ARCHITECTURE.md §4): ~9.2k transcript-line
entries across ~516 videos, 1.9 MB, changes roughly quarterly — so it is
fetched with a conditional GET (stored ETag) on a daily schedule, and entries
are deduped per machine. The `machine` field is dirty ("HackTheBox - X",
"HackThebox - X", "HackTheBox   X"…), hence the tolerant prefix regex; 17
Academy entries carry no videoId at all and are skipped.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any

from sqlalchemy import func, select

from hackqueue.adapters.base import Platform
from hackqueue.adapters.htb import HTBAdapter
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.config import Settings
from hackqueue.db.models import AccountLink, CatalogBox, Solve, utcnow
from hackqueue.db.repo import kv_get, kv_set
from hackqueue.db.session import Database
from hackqueue.http.client import HttpClient
from hackqueue.log import get_logger

log = get_logger(__name__)

IPPSEC_DATASET_URL = "https://raw.githubusercontent.com/IppSec/ippsec.github.io/master/dataset.json"
KV_IPPSEC_ETAG = "catalog:ippsec_etag"

_HTB_PREFIX_RE = re.compile(r"^\s*hackthebox[\s\-:]+", re.IGNORECASE)


def normalize_box_name(name: str) -> str:
    """Alnum-only lowercase — the join key between HTB names and dataset entries."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def extract_htb_machine(machine_field: str) -> str | None:
    """'HackTheBox -  Aragog' → 'Aragog'; non-HTB entries (VulnHub/UHC/…) → None."""
    stripped, n = _HTB_PREFIX_RE.subn("", machine_field)
    return stripped.strip() or None if n else None


def ippsec_video_map(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Dedupe dataset entries to {normalized_htb_machine_name: videoId}."""
    videos: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or "videoId" not in entry:
            continue  # Academy entries and malformed rows
        machine = extract_htb_machine(str(entry.get("machine", "")))
        if not machine:
            continue
        videos.setdefault(normalize_box_name(machine), str(entry["videoId"]))
    return videos


class CatalogService:
    def __init__(
        self, db: Database, http: HttpClient, adapters: AdapterRegistry, settings: Settings
    ) -> None:
        self._db = db
        self._http = http
        self._adapters = adapters
        self._refresh_hours = settings.catalog_refresh_hours
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="catalog:sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(random.uniform(60, 180))  # let pollers settle first
        while True:
            await self.sync()
            await asyncio.sleep(self._refresh_hours * 3600 * random.uniform(0.95, 1.05))

    async def sync(self) -> None:
        # Each source is guarded independently: no HTB token still leaves
        # ippsec data usable, and vice versa.
        try:
            await self.sync_htb_machines()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("catalog_htb_sync_failed")
        try:
            await self.sync_ippsec()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("catalog_ippsec_sync_failed")

    async def sync_htb_machines(self) -> None:
        adapter = self._adapters.get(Platform.HTB)
        if not isinstance(adapter, HTBAdapter):
            return
        count = 0
        async for machine in adapter.iter_machines():
            await self._upsert_box(Platform.HTB.value, machine)
            count += 1
        log.info("catalog_htb_synced", machines=count)

    async def sync_ippsec(self) -> None:
        async with self._db.session() as session:
            stored = await kv_get(session, KV_IPPSEC_ETAG) or {}
        headers = {"If-None-Match": stored["etag"]} if stored.get("etag") else {}
        result = await self._http.get(IPPSEC_DATASET_URL, headers=headers)
        if result.status == 304:
            log.debug("catalog_ippsec_unchanged")
            return
        if result.status != 200 or not isinstance(result.data, list):
            log.warning("catalog_ippsec_unexpected", status=result.status)
            return
        videos = ippsec_video_map(result.data)
        updated = 0
        async with self._db.session() as session, session.begin():
            boxes = await session.scalars(
                select(CatalogBox).where(CatalogBox.platform == Platform.HTB.value)
            )
            for box in boxes:
                video_id = videos.get(box.name_normalized)
                if video_id and box.ippsec_video_id != video_id:
                    box.ippsec_video_id = video_id
                    updated += 1
            if etag := result.headers.get("etag"):
                await kv_set(session, KV_IPPSEC_ETAG, {"etag": etag})
        log.info("catalog_ippsec_synced", videos=len(videos), boxes_updated=updated)

    async def _upsert_box(self, platform: str, data: dict[str, Any]) -> None:
        async with self._db.session() as session, session.begin():
            box = await session.scalar(
                select(CatalogBox).where(
                    CatalogBox.platform == platform,
                    CatalogBox.platform_ref == data["platform_ref"],
                )
            )
            if box is None:
                box = CatalogBox(platform=platform, platform_ref=data["platform_ref"], url="")
                session.add(box)
            box.name = data["name"]
            box.name_normalized = normalize_box_name(data["name"])
            box.os = data.get("os")
            box.difficulty = data.get("difficulty")
            box.tags = data.get("tags") or []
            box.retired = bool(data.get("retired"))
            box.stars = data.get("stars")
            box.release_date = data.get("release_date")
            box.url = data.get("url") or box.url
            box.updated_at = utcnow()

    # ── queries ──────────────────────────────────────────────────────────

    async def suggest(
        self,
        *,
        platform: str = "htb",
        difficulty: str | None = None,
        os: str | None = None,
        tag: str | None = None,
        exclude_refs: set[str] | None = None,
        include_retired: bool = True,
        limit: int = 5,
    ) -> list[CatalogBox]:
        stmt = select(CatalogBox).where(CatalogBox.platform == platform)
        if difficulty:
            stmt = stmt.where(CatalogBox.difficulty == difficulty.lower())
        if os:
            stmt = stmt.where(func.lower(CatalogBox.os) == os.lower())
        if not include_retired:
            stmt = stmt.where(CatalogBox.retired.is_(False))
        async with self._db.session() as session:
            boxes = list(await session.scalars(stmt))
        if tag:
            tag_l = tag.lower()
            boxes = [b for b in boxes if any(tag_l in t.lower() for t in b.tags)]
        if exclude_refs:
            boxes = [b for b in boxes if b.platform_ref not in exclude_refs]
        random.shuffle(boxes)
        return boxes[:limit]

    async def find_box(self, name: str) -> CatalogBox | None:
        normalized = normalize_box_name(name)
        if not normalized:
            return None
        async with self._db.session() as session:
            box = await session.scalar(
                select(CatalogBox).where(CatalogBox.name_normalized == normalized)
            )
            if box is not None:
                return box
            return await session.scalar(
                select(CatalogBox)
                .where(CatalogBox.name_normalized.like(f"%{normalized}%"))
                .order_by(func.length(CatalogBox.name_normalized))
            )

    async def owned_refs(self, discord_user_id: int, platform: str) -> set[str]:
        """item_refs this user has any solve event for (used to exclude owned
        boxes from /suggest). HTB machine ids == catalog platform_refs."""
        async with self._db.session() as session:
            rows = await session.execute(
                select(Solve.item_ref)
                .join(AccountLink, AccountLink.id == Solve.link_id)
                .where(
                    AccountLink.discord_user_id == discord_user_id,
                    Solve.platform == platform,
                )
            )
            return {ref for (ref,) in rows}

    async def box_of_week(self) -> CatalogBox | None:
        """A random non-insane box, preferring ones with a walkthrough video."""
        candidates = await self.suggest(limit=50)
        candidates = [b for b in candidates if b.difficulty in (None, "easy", "medium")]
        with_video = [b for b in candidates if b.ippsec_video_id]
        pool = with_video or candidates
        return random.choice(pool) if pool else None
