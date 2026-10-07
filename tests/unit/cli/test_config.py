"""Loading config.yaml and .env from the Mertina home."""

import os
from pathlib import Path

import pytest

from mertina.cli import config


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_paths_live_under_the_mertina_home(mertina_home: Path) -> None:
    assert config.get_config_path() == mertina_home / "config.yaml"
    assert config.get_env_path() == mertina_home / ".env"


def test_without_a_file_the_defaults_load() -> None:
    cfg = config.load_config()

    assert cfg["model"] == ""
    assert cfg["agent"] == {
        "api_max_retries": 3,
        "tool_use_enforcement": "auto",
        "execution_guidance": "auto",
    }
    assert cfg["web"] == {"backend": "", "search_backend": ""}
    assert cfg["timezone"] == ""


def test_user_values_merge_over_the_defaults(mertina_home: Path) -> None:
    _write(
        mertina_home / "config.yaml", "agent:\n  api_max_retries: 5\nweb:\ntimezone: Asia/Tokyo\n"
    )

    cfg = config.load_config()

    assert cfg["agent"]["api_max_retries"] == 5
    assert cfg["agent"]["tool_use_enforcement"] == "auto"  # sibling default kept
    assert cfg["web"] == {"backend": "", "search_backend": ""}  # an empty section is ignored
    assert cfg["timezone"] == "Asia/Tokyo"


def test_root_max_turns_moves_under_agent(mertina_home: Path) -> None:
    _write(mertina_home / "config.yaml", "max_turns: 12\n")

    cfg = config.load_config()

    assert cfg["agent"]["max_turns"] == 12
    assert "max_turns" not in cfg


@pytest.mark.parametrize(
    ("text", "model"),
    [
        ("model: gpt-x\n", "gpt-x"),  # a bare string stays a string, as upstream loads it
        (
            "model:\n  name: gpt-x\n  api_base: http://h/v1\n",
            {"default": "gpt-x", "base_url": "http://h/v1"},
        ),
        (
            "model:\n  default:\n    model: gpt-x\n    provider: p\n",
            {"default": "gpt-x", "provider": "p"},
        ),
        (
            "base_url: http://h/v1\nmodel:\n  default: gpt-x\n",
            {"default": "gpt-x", "base_url": "http://h/v1"},
        ),
    ],
)
def test_the_model_section_is_canonicalized(
    mertina_home: Path, text: str, model: str | dict[str, str]
) -> None:
    _write(mertina_home / "config.yaml", text)

    assert config.load_config()["model"] == model


def test_env_refs_expand(mertina_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERTINA_TEST_MODEL", "gpt-x")
    _write(
        mertina_home / "config.yaml",
        "model:\n  default: ${MERTINA_TEST_MODEL}\n"
        "web:\n  backend: ${env:MERTINA_TEST_MISSING}\n  search_backend: ${vault:k}\n",
    )

    cfg = config.load_config()

    assert cfg["model"]["default"] == "gpt-x"
    assert cfg["web"]["backend"] == "${env:MERTINA_TEST_MISSING}"  # unresolved refs stay verbatim
    assert cfg["web"]["search_backend"] == "${vault:k}"


def test_a_changed_env_ref_invalidates_the_cache(
    mertina_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERTINA_TEST_MODEL", "a")
    _write(mertina_home / "config.yaml", "model:\n  default: ${MERTINA_TEST_MODEL}\n")
    assert config.load_config()["model"]["default"] == "a"

    monkeypatch.setenv("MERTINA_TEST_MODEL", "b")

    assert config.load_config()["model"]["default"] == "b"


def test_readonly_loads_share_one_object_and_load_config_copies(mertina_home: Path) -> None:
    _write(mertina_home / "config.yaml", "timezone: UTC\n")

    first = config.load_config_readonly()

    assert config.load_config_readonly() is first
    assert config.load_config() is not first
    assert config.load_config() == first


def test_a_broken_file_keeps_the_last_good_config(
    mertina_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = mertina_home / "config.yaml"
    _write(path, "timezone: Asia/Tokyo\n")
    assert config.load_config()["timezone"] == "Asia/Tokyo"

    _write(path, "timezone: [unclosed\n")
    os.utime(path, ns=(1, 1))  # a new signature even on coarse-mtime filesystems

    assert config.load_config()["timezone"] == "Asia/Tokyo"
    err = capsys.readouterr().err
    assert "has a formatting error at line" in err
    assert "loaded before the edit" in err
    assert "Open it in an editor and fix line" in err


def test_a_broken_file_in_a_fresh_process_falls_back_to_the_defaults(
    mertina_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(mertina_home / "config.yaml", "timezone: [unclosed\n")

    assert config.load_config()["timezone"] == ""
    assert "running on default settings" in capsys.readouterr().err


def test_get_env_value_prefers_the_environment(
    mertina_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(mertina_home / ".env", "MERTINA_TEST_KEY=from-file\nMERTINA_TEST_ONLY_FILE=file-only\n")
    monkeypatch.setenv("MERTINA_TEST_KEY", "from-env")
    monkeypatch.delenv("MERTINA_TEST_ONLY_FILE", raising=False)

    assert config.get_env_value("MERTINA_TEST_KEY") == "from-env"
    assert config.get_env_value("MERTINA_TEST_ONLY_FILE") == "file-only"
    assert config.get_env_value("MERTINA_TEST_NOWHERE") is None


def test_load_env_reads_the_env_file(mertina_home: Path) -> None:
    _write(mertina_home / ".env", "A=1\n")

    assert config.load_env() == {"A": "1"}
