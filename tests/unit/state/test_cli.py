"""The schema CLI routes explicit commands and emits only safe failures."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mertina_agent.exceptions import DatabaseError, StorageError
from mertina_agent.state import __main__ as cli


@pytest.mark.parametrize("command", ["init", "check"])
def test_cli_checks_or_bootstraps_with_the_requested_env_file(monkeypatch, capsys, command):
    database = AsyncMock()
    database.__aenter__.return_value = database
    database.backend = "sqlite"
    open_database = AsyncMock(return_value=database)
    initialize = AsyncMock()
    paths = []
    monkeypatch.setattr(cli, "load_settings", lambda *, env_file: paths.append(env_file))
    monkeypatch.setattr(cli, "open_database", open_database)
    monkeypatch.setattr(cli.Schema, "initialize", initialize)
    assert cli.main([command, "--env-file", "custom.env"]) == 0
    assert paths == [Path("custom.env")]
    initialize.assert_awaited_once_with(database, bootstrap=command == "init")
    database.__aexit__.assert_awaited_once()
    assert capsys.readouterr().out == "sqlite: session schema v1 ready\n"


@pytest.mark.parametrize("error", [DatabaseError("connection"), StorageError("Schema missing")])
def test_cli_failure_does_not_report_success_or_exception_details(monkeypatch, capsys, error):
    error.__cause__ = RuntimeError("secret://password@host/query")
    monkeypatch.setattr(cli, "_execute", AsyncMock(side_effect=error))
    assert cli.main(["check"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "secret" not in output.err
    assert "Traceback" not in output.err


def test_cli_rejects_nonexistent_migration_command():
    with pytest.raises(SystemExit) as error:
        cli.main(["migrate"])
    assert error.value.code == 2
