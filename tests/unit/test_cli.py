"""Developer CLI: argument parsing, env-file lookup and command dispatch."""

from pathlib import Path

import pytest

import mertina_agent.gateway.__main__ as gateway_entry
import mertina_agent.state.__main__ as state_entry
from mertina_agent.cli import _title, build_parser, format_session, main, resolve_env_file
from mertina_agent.exceptions import ConfigurationError
from mertina_agent.state.models import SessionRecord


def test_explicit_env_file_must_exist(tmp_path):
    with pytest.raises(ConfigurationError, match="Env file not found"):
        resolve_env_file(tmp_path / "missing.env", home=tmp_path)
    present = tmp_path / "custom.env"
    present.write_text("")
    assert resolve_env_file(present, home=tmp_path) == present


def test_project_env_file_wins_over_user_env_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    home = tmp_path / "home"
    (home / ".mertina").mkdir(parents=True)
    project.mkdir()
    monkeypatch.chdir(project)
    assert resolve_env_file(None, home=home) is None

    (home / ".mertina" / ".env").write_text("")
    assert resolve_env_file(None, home=home) == home / ".mertina" / ".env"

    (project / ".env").write_text("")
    assert resolve_env_file(None, home=home) == Path(".env")


@pytest.mark.parametrize(
    ("argv", "command"),
    [([], None), (["chat"], "chat"), (["run"], "run"), (["sessions"], "sessions")],
)
def test_bare_command_and_run_alias_parse(argv, command):
    assert build_parser().parse_args(argv).command == command


@pytest.mark.parametrize("argv", [["--env-file", "x.env", "chat"], ["chat", "--env-file", "x.env"]])
def test_env_file_is_accepted_before_or_after_the_command(argv):
    assert str(build_parser().parse_args(argv).env_file) == "x.env"


def test_title_collapses_whitespace_and_truncates():
    assert _title("  hello\n  world ") == "hello world"
    long = _title("x" * 100)
    assert len(long) == 60
    assert long.endswith("…")


def test_session_line_escapes_control_characters():
    session = SessionRecord("a" * 32, "bad\x1b[2Jtitle", 0, 0, 1, 0, 3)
    line = format_session(session)
    assert line.startswith("a" * 32)
    assert "\x1b" not in line
    assert "3 msgs" in line


def test_gateway_and_db_delegate_with_the_resolved_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "dev.env"
    env_file.write_text("")
    calls = []
    monkeypatch.setattr(gateway_entry, "main", lambda argv: calls.append(("gateway", argv)) or 0)
    monkeypatch.setattr(state_entry, "main", lambda argv: calls.append(("db", argv)) or 3)

    assert main(["--env-file", str(env_file), "gateway"]) == 0
    assert main(["db", "init", "--env-file", str(env_file)]) == 3
    assert calls == [
        ("gateway", ["--env-file", str(env_file)]),
        ("db", ["init", "--env-file", str(env_file)]),
    ]


def test_missing_explicit_env_file_is_reported(tmp_path, capsys):
    assert main(["--env-file", str(tmp_path / "missing.env"), "sessions"]) == 1
    assert "Env file not found" in capsys.readouterr().err
