"""Retry-After parsing and jittered backoff (Hermes retry_utils intent)."""

import random
from datetime import UTC, datetime

import pytest

from mertina_agent.agent.retry_utils import jittered_backoff, parse_retry_after_seconds

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        (" 2.5 ", 2.5),
        (3, 3.0),
        (1.5, 1.5),
        ("-4", 0.0),
        (-4, 0.0),
        ("Tue, 22 Sep 2026 12:00:30 GMT", 30.0),
        ("Tue, 22 Sep 2026 11:59:00 GMT", 0.0),
        ("soon", None),
        ("", None),
        (None, None),
        (True, None),
        ({"Retry-After": "9"}, 9.0),
        ({"retry-after": "11"}, 11.0),
        ({"x-other": "1"}, None),
    ],
)
def test_retry_after_values_are_parsed_into_seconds(value, expected):
    assert parse_retry_after_seconds(value, now=lambda: NOW) == expected


def test_retry_after_date_without_zone_is_read_as_utc():
    assert parse_retry_after_seconds("Tue, 22 Sep 2026 12:01:00 -0000", now=lambda: NOW) == 60.0


def test_backoff_doubles_per_attempt_plus_bounded_jitter():
    rng = random.Random(1)

    delays = [
        jittered_backoff(attempt, base_delay=2.0, max_delay=60.0, rng=rng) for attempt in (1, 2, 3)
    ]

    for delay, base in zip(delays, (2.0, 4.0, 8.0), strict=True):
        assert base <= delay <= base * 1.5


def test_backoff_is_capped_at_the_maximum():
    delay = jittered_backoff(30, base_delay=2.0, max_delay=60.0, rng=random.Random(1))

    assert 60.0 <= delay <= 90.0


@pytest.mark.parametrize(("attempt", "base_delay"), [(64, 2.0), (1, 0.0)])
def test_extreme_attempts_or_zero_base_fall_back_to_the_maximum(attempt, base_delay):
    delay = jittered_backoff(
        attempt, base_delay=base_delay, max_delay=10.0, jitter_ratio=0.0, rng=random.Random(1)
    )

    assert delay == 10.0


def test_seeded_randomness_makes_backoff_reproducible():
    first = jittered_backoff(2, rng=random.Random(42))
    second = jittered_backoff(2, rng=random.Random(42))

    assert first == second


def test_default_randomness_needs_no_seed():
    assert 5.0 <= jittered_backoff(1) <= 7.5


def test_retry_after_date_uses_the_real_clock_by_default():
    assert parse_retry_after_seconds("Fri, 01 Jan 2100 00:00:00 GMT") > 0
