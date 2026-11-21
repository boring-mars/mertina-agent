"""The schema CLI routes explicit commands and emits only safe failures."""

from unittest.mock import AsyncMock

import pytest

from mertina_agent.exceptions import DatabaseError, StorageError
from mertina_agent.state import __main__ as cli


@pytest.mark.parametrize("command", ["init", "check"])
def test_cli_checks_or_bootstraps_with_the_requested_env_file(
    monkeypatch, capsys, tmp_path, command
):
    env_file = tmp_path / "custom.env"
    env_file.write_text("")
    database = AsyncMock()
    database.__aenter__.return_value = database
    database.backend = "sqlite"
    open_database = AsyncMock(return_value=database)
    initialize = AsyncMock()
    paths = []
    monkeypatch.setattr(cli, "load_settings", lambda *, env_file: paths.append(env_file))
    monkeypatch.setattr(cli, "open_database", open_database)
    monkeypatch.setattr(cli.Schema, "initialize", initialize)
    assert cli.main([command, "--env-file", str(env_file)]) == 0
    assert paths == [env_file]
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


def test_cli_without_env_file_uses_the_shared_lookup(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".mertina").mkdir(parents=True)
    (home / ".mertina" / ".env").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    paths = []
    monkeypatch.setattr(cli, "_execute", AsyncMock(side_effect=lambda _c, path: paths.append(path)))
    assert cli.main(["check"]) == 0
    assert paths == [home / ".mertina" / ".env"]


def test_cli_rejects_a_missing_explicit_env_file(tmp_path, capsys):
    assert cli.main(["check", "--env-file", str(tmp_path / "missing.env")]) == 1
    assert "Env file not found" in capsys.readouterr().err
