"""The clock the timestamp line reads: MERTINA_TIMEZONE, then config.yaml, else server-local."""

import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mertina import time as mertina_time


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mertina_time, "_tz_cache", {})
    monkeypatch.delenv("MERTINA_TIMEZONE", raising=False)


def test_mertina_timezone_names_the_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERTINA_TIMEZONE", " Asia/Tokyo ")

    assert mertina_time.get_timezone() == ZoneInfo("Asia/Tokyo")
    assert mertina_time.now().tzinfo == ZoneInfo("Asia/Tokyo")


def test_without_it_the_clock_is_server_local() -> None:
    assert mertina_time.get_timezone() is None
    assert mertina_time.now().tzinfo is not None


def test_an_invalid_zone_falls_back_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MERTINA_TIMEZONE", "Not/AZone")

    with caplog.at_level(logging.WARNING, logger="mertina.time"):
        assert mertina_time.get_timezone() is None

    assert "Invalid timezone 'Not/AZone'" in caplog.text


def test_each_zone_is_resolved_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERTINA_TIMEZONE", "Asia/Tokyo")
    first = mertina_time.get_timezone()
    monkeypatch.setattr(mertina_time, "_resolve_timezone_name", lambda: "Europe/Oslo")

    assert mertina_time.get_timezone() is first


def test_config_yaml_names_the_zone_when_the_env_var_is_unset(mertina_home: Path) -> None:
    (mertina_home / "config.yaml").write_text("timezone: ' Europe/Oslo '\n", encoding="utf-8")

    assert mertina_time.get_timezone() == ZoneInfo("Europe/Oslo")


def test_the_env_var_wins_over_config_yaml(
    mertina_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (mertina_home / "config.yaml").write_text("timezone: Europe/Oslo\n", encoding="utf-8")
    monkeypatch.setenv("MERTINA_TIMEZONE", "Asia/Tokyo")

    assert mertina_time.get_timezone() == ZoneInfo("Asia/Tokyo")


def test_an_unreadable_config_yaml_means_server_local(mertina_home: Path) -> None:
    (mertina_home / "config.yaml").write_text("timezone: [unclosed\n", encoding="utf-8")

    assert mertina_time.get_timezone() is None
