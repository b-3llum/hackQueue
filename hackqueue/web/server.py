"""The public web leaderboard.

Opt-in per guild (``/config web on``): a server that hasn't opted in 404s, so
no member's display name or avatar is ever published without a moderator
turning it on. The page itself is static; all data comes from the JSON API
below, which is the only place that touches the database.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import discord
from aiohttp import web

from hackqueue import __version__
from hackqueue.adapters.base import PLATFORM_LABELS, Platform
from hackqueue.config import Settings
from hackqueue.db.models import Guild, GuildMember, utcnow
from hackqueue.db.session import Database
from hackqueue.log import get_logger
from hackqueue.services.boards import CLAIMS_KEY, Board, BoardService
from hackqueue.services.directory import DirectoryService
from hackqueue.services.profiles import ProfileService
from hackqueue.services.scoring import Period, period_start

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
BOARD_KEYS = [p.value for p in Platform] + [CLAIMS_KEY]
BOARD_LABELS = {**{p.value: label for p, label in PLATFORM_LABELS.items()}, CLAIMS_KEY: "Claims"}
CACHE_SECONDS = 60


class WebServer:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        boards: BoardService,
        directory: DirectoryService,
        client: discord.Client,
        profiles: ProfileService | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._boards = boards
        self._directory = directory
        self._client = client
        self._profiles = profiles
        self._runner: web.AppRunner | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self.handle_index),
                web.get("/g/{guild_id}", self.handle_board_page),
                web.get("/api/g/{guild_id}", self.handle_board_data),
                web.get("/api/g/{guild_id}/member/{user_id}", self.handle_member_data),
                web.get("/healthz", self.handle_healthz),
                web.static("/static", STATIC_DIR),
            ]
        )
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._settings.web_host, self._settings.web_port)
        await site.start()
        log.info("web_started", host=self._settings.web_host, port=self._settings.web_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ── handlers ─────────────────────────────────────────────────────────

    async def handle_healthz(self, _: web.Request) -> web.Response:
        return web.json_response({"ok": True, "version": __version__})

    async def handle_index(self, _: web.Request) -> web.Response:
        # No guild directory is published — you reach a board by its link.
        return web.FileResponse(STATIC_DIR / "index.html")

    async def handle_board_page(self, request: web.Request) -> web.Response:
        guild = await self._published_guild(request)
        if guild is None:
            raise web.HTTPNotFound(text="No published leaderboard here.")
        return web.FileResponse(STATIC_DIR / "board.html")

    async def handle_board_data(self, request: web.Request) -> web.Response:
        guild = await self._published_guild(request)
        if guild is None:
            return web.json_response({"error": "not_published"}, status=404)
        board_key = request.query.get("board", "composite")
        period_key = request.query.get("period", "weekly")
        try:
            period = Period(period_key)
        except ValueError:
            return web.json_response({"error": "bad_period"}, status=400)

        board = await self._load_board(guild.guild_id, board_key, period)
        if board is None:
            return web.json_response({"error": "bad_board"}, status=400)

        identities = await self._directory.identities(
            guild.guild_id, [row.discord_user_id for row in board.rows]
        )
        # Where everyone stood at the end of the previous period, so the board
        # can show who is climbing rather than just who is ahead.
        previous = await self._previous_positions(guild.guild_id, board_key, period)
        discord_guild = self._client.get_guild(guild.guild_id)
        rows = []
        for position, row in enumerate(board.rows, start=1):
            identity = identities.get(row.discord_user_id)
            was = previous.get(row.discord_user_id)
            rows.append(
                {
                    "rank": position,
                    "user_id": str(row.discord_user_id),
                    "name": identity.display_name if identity else "Unknown member",
                    "avatar": identity.avatar_url if identity else None,
                    "handle": row.label or None,  # platform username, when relevant
                    "value": round(row.value, 1),
                    "verified": row.verified,
                    "parts": {k: round(v, 2) for k, v in row.parts.items() if v > 0},
                    # positive = climbed N places since last period; None = new
                    "movement": (was - position) if was is not None else None,
                }
            )
        payload = {
            "guild": {
                "name": discord_guild.name if discord_guild else "This server",
                "icon": discord_guild.icon.url if discord_guild and discord_guild.icon else None,
            },
            "board": board_key,
            "period": period.value,
            "summary": {
                "members": len(rows),
                "active": sum(1 for r in rows if r["value"] > 0),
                "total": round(sum(r["value"] for r in rows), 1),
                "unit": "index" if board_key == "composite" else "points",
            },
            "rows": rows,
            "stale": [BOARD_LABELS.get(p, p) for p in board.stale_platforms],
            "boards": [{"key": "composite", "label": "Composite"}]
            + [{"key": k, "label": BOARD_LABELS[k]} for k in BOARD_KEYS],
            "platform_labels": BOARD_LABELS,
            "generated_at": utcnow().isoformat(),
        }
        return web.json_response(
            payload, headers={"Cache-Control": f"public, max-age={CACHE_SECONDS}"}
        )

    async def handle_member_data(self, request: web.Request) -> web.Response:
        """Everything behind clicking a name: per-platform scores, a score
        series, recent solves, weekly activity."""
        guild = await self._published_guild(request)
        if guild is None or self._profiles is None:
            return web.json_response({"error": "not_published"}, status=404)
        raw = request.match_info.get("user_id", "")
        if not raw.isdigit():
            return web.json_response({"error": "bad_user"}, status=400)
        user_id = int(raw)
        # Only members of THIS guild's board — you can't read a stranger's
        # profile by pasting their Discord id into a published board.
        async with self._db.session() as session:
            member = await session.get(GuildMember, (guild.guild_id, user_id))
        if member is None or member.hidden:
            return web.json_response({"error": "not_a_member"}, status=404)

        detail = await self._profiles.member(guild.guild_id, user_id)
        if detail is None:
            return web.json_response({"error": "no_data"}, status=404)
        identities = await self._directory.identities(guild.guild_id, [user_id])
        identity = identities.get(user_id)
        return web.json_response(
            {
                "name": identity.display_name if identity else "Unknown member",
                "avatar": identity.avatar_url if identity else None,
                "platforms": [
                    {
                        **asdict(p),
                        "label": BOARD_LABELS.get(p.platform, p.platform),
                        "unit": "flags" if p.platform == Platform.HTB.value else "points",
                    }
                    for p in detail.platforms
                ],
                "recent_solves": [asdict(s) for s in detail.recent_solves],
                "activity": detail.activity,
                "claims": {
                    "approved": detail.claims_approved,
                    "points": detail.claims_points,
                },
                "streak_weeks": detail.solve_streak_weeks,
                "total_solves": detail.total_solves,
                "platform_labels": BOARD_LABELS,
            },
            headers={"Cache-Control": f"public, max-age={CACHE_SECONDS}"},
        )

    # ── helpers ──────────────────────────────────────────────────────────

    async def _previous_positions(
        self, guild_id: int, board_key: str, period: Period
    ) -> dict[int, int]:
        """Board positions as of the end of the previous period (empty for
        all-time, which has no previous)."""
        if period is Period.ALLTIME:
            return {}
        start = period_start(period, utcnow())
        if start is None:
            return {}
        board = await self._load_board(
            guild_id, board_key, period, as_of=start - timedelta(seconds=1)
        )
        if board is None:
            return {}
        return {row.discord_user_id: i for i, row in enumerate(board.rows, start=1)}

    async def _load_board(
        self, guild_id: int, board_key: str, period: Period, as_of: datetime | None = None
    ) -> Board | None:
        if board_key == "composite":
            return await self._boards.composite_board(guild_id, period, as_of=as_of)
        if board_key == CLAIMS_KEY:
            return await self._boards.claims_board(guild_id, period, as_of=as_of)
        try:
            platform = Platform(board_key)
        except ValueError:
            return None
        return await self._boards.platform_board(guild_id, platform, period, as_of=as_of)

    async def _published_guild(self, request: web.Request) -> Guild | None:
        raw = request.match_info.get("guild_id", "")
        if not raw.isdigit():
            return None
        async with self._db.session() as session:
            guild = await session.get(Guild, int(raw))
        return guild if guild is not None and guild.web_enabled else None
