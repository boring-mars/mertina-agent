"""The persisted backend selection, read from the raw config.yaml, and its error text."""

from pathlib import Path

import pytest

from mertina.cli.config import read_raw_config_readonly
from mertina.tools import tool_backend_helpers as helpers


def _write_config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_nothing_is_selected_without_a_config() -> None:
    assert helpers.read_selection("web") is None
    assert not helpers.selection_exists("web")


@pytest.mark.parametrize(
    ("web", "selected"),
    [
        ("backend: ' DDGS '", "ddgs"),
        ("use_gateway: true\n  backend: ddgs", helpers.NOUS_MANAGED_PROVIDER),
        ("use_gateway: false", None),
        ("search_backend: ddgs", None),
    ],
)
def test_the_web_selection(mertina_home: Path, web: str, selected: str | None) -> None:
    _write_config(mertina_home, f"web:\n  {web}\n")

    assert helpers.read_selection("web") == selected


def test_a_per_capability_key_counts_as_configured(mertina_home: Path) -> None:
    _write_config(mertina_home, "web:\n  search_backend: ddgs\n")

    assert helpers.selection_exists("web")


def test_defaults_are_not_a_selection(mertina_home: Path) -> None:
    _write_config(mertina_home, "model: gpt-x\n")

    assert read_raw_config_readonly() == {"model": "gpt-x"}
    assert not helpers.selection_exists("web")


def test_a_broken_config_reads_as_empty(mertina_home: Path) -> None:
    _write_config(mertina_home, "web: [unclosed\n")

    assert read_raw_config_readonly() == {}
    assert helpers.read_selection("web") is None


def test_the_raw_read_is_cached_until_the_file_changes(mertina_home: Path) -> None:
    _write_config(mertina_home, "web:\n  backend: a\n")
    first = read_raw_config_readonly()
    assert read_raw_config_readonly() is first

    _write_config(mertina_home, "web:\n  backend: bb\n")
    assert read_raw_config_readonly() == {"web": {"backend": "bb"}}


def test_selection_error_and_removed_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    assert helpers.selection_error("web", "'x'", "it broke") == (
        "web is configured to use 'x' (set in config.yaml), but it broke. Change it in config.yaml."
    )

    monkeypatch.setitem(helpers.REMOVED_BACKENDS, "web", {"x": "x was removed"})
    assert "but x was removed." in helpers.selection_error("web", "'X'", "it broke")
