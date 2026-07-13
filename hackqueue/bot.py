"""Bot wiring: builds every service once and owns startup/shutdown order."""

from __future__ import annotations

import discord
from discord.ext import commands

from hackqueue.adapters.registry import AdapterRegistry, build_http_client, build_registry
from hackqueue.config import Settings
from hackqueue.db.session import Database
from hackqueue.log import get_logger
from hackqueue.services.boards import BoardService
from hackqueue.services.catalog import CatalogService
from hackqueue.services.claims import ClaimsService
from hackqueue.services.directory import DirectoryService
from hackqueue.services.health import HealthRegistry
from hackqueue.services.linking import LinkingService
from hackqueue.services.profiles import ProfileService
from hackqueue.services.recap import RecapService
from hackqueue.services.snapshots import PollerService
from hackqueue.web.server import WebServer

log = get_logger(__name__)

EXTENSIONS = (
    "hackqueue.cogs.setup",
    "hackqueue.cogs.link",
    "hackqueue.cogs.profile",
    "hackqueue.cogs.leaderboard",
    "hackqueue.cogs.claims",
    "hackqueue.cogs.boxes",
    "hackqueue.cogs.admin",
)


class HackQueueBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        # Slash commands only — no privileged intents, no message content.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
        )
        self.settings = settings
        self.scoring_config = settings.scoring()
        self.db = Database(settings.database_url)
        # NB: attribute is http_client — commands.Bot.http is discord.py's own gateway client.
        self.http_client = build_http_client()
        self.adapters: AdapterRegistry = build_registry(self.http_client, settings)
        self.health = HealthRegistry()
        self.linking = LinkingService(self.db, self.adapters)
        self.boards = BoardService(self.db, self.adapters, self.health, self.scoring_config)
        self.claims = ClaimsService(self.db, self.scoring_config)
        self.catalog = CatalogService(self.db, self.http_client, self.adapters, settings)
        self.poller = PollerService(self.db, self.adapters, settings, self.health)
        self.recap = RecapService(self, self.db, self.boards, self.catalog)
        self.directory = DirectoryService(self.db, self)
        self.profiles = ProfileService(self.db, self.adapters)
        self.web = (
            WebServer(settings, self.db, self.boards, self.directory, self, self.profiles)
            if settings.web_enabled
            else None
        )

    async def setup_hook(self) -> None:
        await self.db.migrate()
        await self.http_client.start()
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        synced = await self.tree.sync()
        log.info(
            "bot_ready_to_connect",
            commands_synced=len(synced),
            platforms=[p.value for p in self.adapters.platforms],
        )
        self.poller.start()
        self.catalog.start()
        self.recap.start()
        if self.web is not None:
            await self.web.start()

    async def on_ready(self) -> None:
        log.info("bot_connected", user=str(self.user), guilds=len(self.guilds))

    async def close(self) -> None:
        log.info("bot_shutting_down")
        await self.poller.stop()
        await self.catalog.stop()
        await self.recap.stop()
        if self.web is not None:
            await self.web.stop()
        await self.http_client.close()
        await self.db.close()
        await super().close()
