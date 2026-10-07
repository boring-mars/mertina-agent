"""Where the Mertina home is."""

import sys
from pathlib import Path

import pytest

from mertina import constants


def test_mertina_home_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERTINA_HOME", " ~/somewhere ")

    assert constants.get_mertina_home() == Path("~/somewhere").expanduser()
    assert constants.get_config_path() == Path("~/somewhere").expanduser() / "config.yaml"


def test_without_it_the_platform_default_is_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MERTINA_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    expected = tmp_path / "mertina" if sys.platform == "win32" else tmp_path / ".mertina"
    assert constants.get_mertina_home() == expected
