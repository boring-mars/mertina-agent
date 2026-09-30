# Ported from hermes-agent agent/retry_utils.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Retry utilities — jittered backoff for decorrelated retries.

Jittered delays (vs. fixed exponential) prevent thundering-herd retry spikes
when many sessions hit the same rate-limited provider concurrently.
"""

import random
import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

# Monotonic counter for jitter-seed uniqueness within a process; locked
# because concurrent gateway sessions retry simultaneously.
_jitter_counter = 0
_jitter_lock = threading.Lock()


def parse_retry_after_seconds(value_or_headers: Any) -> float | None:
    """Parse a ``Retry-After`` value (numeric / HTTP-date) or a headers mapping (both casings
    tried) into seconds, clamped at 0.0; None when absent / unparseable."""
    raw = value_or_headers
    if raw is not None and not isinstance(raw, (str, int, float)):
        getter = getattr(raw, "get", None)
        if not callable(getter):
            return None
        try:
            raw = getter("Retry-After")
            if raw is None:
                raw = getter("retry-after")
        except Exception:
            return None
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    # HTTP-date form (RFC 7231): seconds until that instant, clamped at 0.
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:  # older stdlib returns None instead of raising
        return None  # type: ignore[unreachable]  # typeshed follows the stdlib that raises
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def jittered_backoff(
    attempt: int, *, base_delay: float = 5.0, max_delay: float = 120.0, jitter_ratio: float = 0.5
) -> float:
    """min(base * 2^(attempt-1), max_delay) + uniform jitter in
    [0, jitter_ratio * delay]. ``attempt`` is 1-based."""
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    delay = (
        max_delay
        if (exponent >= 63 or base_delay <= 0)
        else min(base_delay * (2**exponent), max_delay)
    )

    # Seed from time + counter so coarse clocks still decorrelate.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    return delay + random.Random(seed).uniform(0, jitter_ratio * delay)
