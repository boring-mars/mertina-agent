"""Fail closed when a selected driver's installation is unavailable."""

import asyncio

import pytest
from pydantic import SecretStr

from mertina_agent.config import Settings
from mertina_agent.database.postgresql import adapter
from mertina_agent.database.runtime import open_database
from mertina_agent.exceptions import ConfigurationError


def test_missing_driver_does_not_fall_back_to_sqlite(monkeypatch, tmp_path):
    def missing_driver(*_args):
        message = "test missing driver"
        raise ModuleNotFoundError(message)

    monkeypatch.setattr(adapter, "create_engine", missing_driver)
    settings = Settings(database_url=SecretStr("postgresql://user:private@localhost/db"))
    with pytest.raises(ConfigurationError) as error:
        asyncio.run(open_database(settings))
    assert "private" not in str(error.value)
    assert not list(tmp_path.iterdir())
