"""The clock the timestamp line reads: MERTINA_TIMEZONE, else server-local time."""

import logging
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
