"""HTB Seasons — the weekly machine drops.

Three features, all live-verified 2026-07-13:

- **Drop watcher**: polls the active season's live machine and fires once when
  a new one releases (Saturdays 19:00 UTC). The announcer turns that into a
  "the box just dropped — go" post in each server's announce channel.
- **Season status** (`/season`): which season, which week, the live machine.
- **Season leaderboard**: how far each *server member* is through this
  season's boxes — built from OUR OWN solve data (the machine ids the poller
  already records), not from HTB's per-user season endpoint, which the single
  bot token can only read for itself. So it works for every member, honestly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from hackqueue.adapters.base import Platform
from hackqueue.adapters.htb import HTBAdapter, _parse_date
from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.db.models import AccountLink, GuildMember, Solve
from hackqueue.db.session import Database
from hackqueue.log import get_logger

log = get_logger(__name__)

MACHINE_WEB_URL = "https://app.hackthebox.com/machines/{ref}"


@dataclass(frozen=True)
class SeasonMachine:
    machine_id: str
    name: str
    os: str | None
    difficulty: str | None
    released: bool
    release_time: datetime | None
    url: str


@dataclass(frozen=True)
class Season:
    season_id: int
    name: str
    subtitle: str
    current_week: int | None
    total_weeks: int | None
    ends_at: datetime | None
    players: int
    live_machine: SeasonMachine | None
    machines: list[SeasonMachine] = field(default_factory=list)

    @property
    def released_machines(self) -> list[SeasonMachine]:
        return [m for m in self.machines if m.released]


@dataclass(frozen=True)
class SeasonStanding:
    discord_user_id: int
    username: str
    owned: int  # season machines this member has rooted
    total: int


class SeasonService:
    def __init__(self, db: Database, adapters: AdapterRegistry) -> None:
        self._db = db
        self._adapters = adapters

    @property
    def _htb(self) -> HTBAdapter | None:
        adapter = self._adapters.get(Platform.HTB)
        return adapter if isinstance(adapter, HTBAdapter) else None

    async def current(self) -> Season | None:
        """The active season with its schedule and live machine, or None
        (HTB disabled, between seasons, or the API is unreachable)."""
        htb = self._htb
        if htb is None:
            return None
        raw = await htb.active_season()
        if raw is None:
            return None
        season_id = int(raw["id"])
        machines = [_machine(m) for m in await htb.season_machines(season_id)]
        live_raw = await htb.active_season_machine()
        live = _machine(live_raw) if live_raw else None
        return Season(
            season_id=season_id,
            name=str(raw.get("name", "Season")),
            subtitle=str(raw.get("subtitle", "")),
            current_week=_int_or_none(raw.get("current_week")),
            total_weeks=_int_or_none(raw.get("weeks")),
            ends_at=_parse_date(raw.get("end_date")),
            players=_int(raw.get("players")),
            live_machine=live,
            machines=machines,
        )

    async def standings(self, guild_id: int, season: Season) -> list[SeasonStanding]:
        """How many of this season's *released* machines each guild member has
        rooted. Uses the ``root`` solves the poller already stores, so it's the
        same view for everyone regardless of whose token the bot holds."""
        released_ids = {m.machine_id for m in season.released_machines}
        if not released_ids:
            return []
        async with self._db.session() as session:
            links = list(
                await session.scalars(
                    select(AccountLink)
                    .join(
                        GuildMember,
                        GuildMember.discord_user_id == AccountLink.discord_user_id,
                    )
                    .where(
                        GuildMember.guild_id == guild_id,
                        GuildMember.hidden.is_(False),
                        AccountLink.platform == Platform.HTB.value,
                    )
                )
            )
            standings = []
            for link in links:
                owned_ids = {
                    ref
                    for (ref,) in await session.execute(
                        select(Solve.item_ref).where(
                            Solve.link_id == link.id,
                            Solve.kind == "root",
                            Solve.item_ref.in_(released_ids),
                        )
                    )
                }
                standings.append(
                    SeasonStanding(
                        discord_user_id=link.discord_user_id,
                        username=link.platform_username,
                        owned=len(owned_ids),
                        total=len(released_ids),
                    )
                )
        standings.sort(key=lambda s: s.owned, reverse=True)
        return standings


def _machine(raw: dict) -> SeasonMachine:
    machine_id = str(raw.get("id", ""))
    # The live-machine endpoint uses difficulty_text; the schedule uses the
    # same key. is_released is absent on unreleased future weeks (use .get).
    return SeasonMachine(
        machine_id=machine_id,
        name=str(raw.get("name", "?")),
        os=(raw.get("os") or None),
        difficulty=(raw.get("difficulty_text") or "").lower() or None,
        released=bool(raw.get("is_released") or raw.get("active")),
        release_time=_parse_date(raw.get("release_time") or raw.get("release")),
        url=MACHINE_WEB_URL.format(ref=machine_id),
    )


def _int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
