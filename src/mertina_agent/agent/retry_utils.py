"""Retry helpers: ``Retry-After`` parsing and jittered exponential backoff.

Copied from Hermes agent/retry_utils.py (``parse_retry_after_seconds``,
``jittered_backoff``) at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md.

Jittered delays (rather than fixed exponential ones) keep many clients hitting
the same rate-limited provider from retrying in lockstep. Hermes seeds its jitter
from the wall clock; here the clock and the random source are parameters so the
behavior is deterministic under test. The quota-text grammars and the Z.AI
adaptive policy are left out.
"""

import random
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

_SYSTEM_RANDOM = random.SystemRandom()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def parse_retry_after_seconds(
    value_or_headers: str | int | float | Mapping[str, str] | None,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> float | None:
    """Parse a ``Retry-After`` value, numeric or HTTP-date, into seconds.

    A headers mapping is also accepted (both casings are tried). The result is
    clamped at 0.0; ``None`` means the value is absent or unparseable.
    """
    raw: object = value_or_headers
    if isinstance(value_or_headers, Mapping):
        raw = value_or_headers.get("Retry-After")
        if raw is None:
            raw = value_or_headers.get("retry-after")
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return max(0.0, float(raw))
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    # HTTP-date form (RFC 9110): seconds until that instant, clamped at 0.
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now()).total_seconds())


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
    rng: random.Random = _SYSTEM_RANDOM,
) -> float:
    """Return ``min(base * 2^(attempt-1), max_delay)`` plus uniform jitter.

    The jitter is drawn from ``[0, jitter_ratio * delay]``; ``attempt`` is 1-based.
    """
    exponent = max(0, attempt - 1)
    delay = (
        max_delay
        if exponent >= 63 or base_delay <= 0
        else min(base_delay * (2**exponent), max_delay)
    )
    return delay + rng.uniform(0, jitter_ratio * delay)
