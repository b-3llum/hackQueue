"""Async engine/session factory and startup migrations."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from hackqueue.log import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _enable_sqlite_fks(engine: AsyncEngine) -> None:
    # SQLite ships with foreign keys off; ON DELETE CASCADE (the /unlink purge)
    # silently does nothing without this pragma.
    @event.listens_for(engine.sync_engine, "connect")
    def _fk_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        if url.startswith("sqlite"):
            db_path = url.rsplit("///", 1)[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url)
        if self.engine.dialect.name == "sqlite":
            _enable_sqlite_fks(self.engine)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def migrate(self) -> None:
        """Apply Alembic migrations to head.

        Runs in a worker thread: alembic's command API is sync, and our async
        env.py calls asyncio.run(), which must not happen on the bot's loop.
        """
        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.set_main_option("sqlalchemy.url", self.url)
        await asyncio.to_thread(command.upgrade, cfg, "head")
        log.info("db_migrated", url=self.engine.url.render_as_string(hide_password=True))

    async def close(self) -> None:
        await self.engine.dispose()
