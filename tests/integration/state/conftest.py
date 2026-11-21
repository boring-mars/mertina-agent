"""Explicit test targets with isolated namespaces, removed after every scenario."""

import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from pydantic import SecretStr

from mertina_agent.config import Settings
from mertina_agent.database.runtime import open_database
from mertina_agent.state.schema import Schema
from mertina_agent.state.session_db import SessionDB

_TARGETS = {
    "postgresql": os.environ.get("MERTINA_TEST_POSTGRESQL_URL"),
    "mysql": os.environ.get("MERTINA_TEST_MYSQL_URL"),
}


@pytest.fixture(params=["sqlite", "postgresql", "mysql"])
def state_settings(request, tmp_path):
    backend = request.param
    url = (
        "sqlite+aiosqlite:///" + str(tmp_path / "state.sqlite3")
        if backend == "sqlite"
        else _TARGETS[backend]
    )
    if not url:
        pytest.skip(f"Set MERTINA_TEST_{backend.upper()}_URL to run this backend")
    return Settings(database_url=SecretStr(url), database_connect_timeout_s=3)


@pytest.fixture
def storage_factory(state_settings):
    @asynccontextmanager
    async def factory(*, bootstrap=True):
        database = await open_database(state_settings)
        schema = Schema(prefix="ms_" + uuid4().hex + "_")
        try:
            if bootstrap:
                await schema.initialize(database, bootstrap=True)
            yield SessionDB(database, schema=schema)
        finally:
            await database.aclose()
            async with (
                await open_database(state_settings) as cleanup,
                cleanup.transaction() as connection,
            ):
                await connection.run_sync(schema.metadata.drop_all)

    return factory
