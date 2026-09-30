"""Retry-After parsing and jittered backoff."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

from mertina.agent.retry_utils import jittered_backoff, parse_retry_after_seconds


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("7", 7.0),
        (" 2.5 ", 2.5),
        (3, 3.0),
        (-4, 0.0),
        ({"Retry-After": "9"}, 9.0),
        ({"retry-after": "11"}, 11.0),
        ({}, None),
        (None, None),
        (True, None),
        ("", None),
        ("soon", None),
        (object(), None),
    ],
)
def test_parse_retry_after_seconds(value: Any, seconds: float | None) -> None:
    assert parse_retry_after_seconds(value) == seconds


def test_an_http_date_is_seconds_from_now() -> None:
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    earlier = format_datetime(datetime.now(UTC) - timedelta(seconds=90), usegmt=True)

    assert 80 < (parse_retry_after_seconds(later) or 0) <= 90
    assert parse_retry_after_seconds(earlier) == 0.0


def test_jittered_backoff_doubles_up_to_the_cap_plus_jitter() -> None:
    assert 2.0 <= jittered_backoff(1, base_delay=2.0, max_delay=60.0) <= 3.0
    assert 8.0 <= jittered_backoff(3, base_delay=2.0, max_delay=60.0) <= 12.0
    assert 60.0 <= jittered_backoff(20, base_delay=2.0, max_delay=60.0) <= 90.0
    assert jittered_backoff(1, base_delay=0, max_delay=5.0, jitter_ratio=0) == 5.0
