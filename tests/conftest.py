from __future__ import annotations

import pytest

from hackqueue.db.models import Base
from hackqueue.db.session import Database
from hackqueue.http.client import HttpClient


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield database
    await database.close()


@pytest.fixture
async def http():
    client = HttpClient(user_agent="hackQueue-tests/0", base_backoff=0.001)
    await client.start()
    yield client
    await client.close()
