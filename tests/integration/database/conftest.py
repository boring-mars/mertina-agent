"""Explicitly selected network targets; per-test tables never touch application data."""

import os

import pytest
from pydantic import SecretStr

from mertina_agent.config import Settings

# Capture explicit test targets before the repository's autouse environment cleanup.
_TARGETS = {
    "postgresql": os.environ.get("MERTINA_TEST_POSTGRESQL_URL"),
    "mysql": os.environ.get("MERTINA_TEST_MYSQL_URL"),
}


@pytest.fixture(params=["sqlite", "postgresql", "mysql"])
def database_settings(request, tmp_path):
    backend = request.param
    if backend == "sqlite":
        url = "sqlite+aiosqlite:///" + str(tmp_path / "nested" / "database.sqlite3")
    else:
        url = _TARGETS[backend]
        if not url:
            pytest.skip(f"Set MERTINA_TEST_{backend.upper()}_URL to run this backend")
    return Settings(database_url=SecretStr(url), database_connect_timeout_s=3)
