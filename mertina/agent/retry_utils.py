# Ported from hermes-agent agent/retry_utils.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Retry utilities — jittered backoff for decorrelated retries.

Jittered delays (vs. fixed exponential) prevent thundering-herd retry spikes
when many sessions hit the same rate-limited provider concurrently.
"""

import random
import re
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


# Free-text "reset" grammars providers put in error bodies, tried in order. One table so the
# conversation loop's error context and the credential pool's cooldown agree on the same wait.
_QUOTA_RESET_DELAY_RE = re.compile(r"quotaResetDelay[:\s\"]+(\d+(?:\.\d+)?)(ms|s)", re.IGNORECASE)
# "Resets in 4hr 5min" (weekly usage limits), "resets in 2 hours 5 minutes", "resets in 30s".
_RESETS_IN_RE = re.compile(
    r"resets?\s+in\s+"
    r"(?:(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b)?",
    re.IGNORECASE,
)
_RETRY_AFTER_SECONDS_RE = re.compile(
    r"retry\s+(?:after\s+)?(\d+(?:\.\d+)?)\s*(?:sec|secs|seconds|s\b)", re.IGNORECASE
)
# The plan usage-limit body field as it appears once stringified: ``'resets_in_seconds': 30995``.
_RESETS_IN_SECONDS_FIELD_RE = re.compile(r"resets_in_seconds\W{1,4}(\d+(?:\.\d+)?)", re.IGNORECASE)


def _quota_reset_seconds(m: "re.Match[str]") -> float:
    value = float(m.group(1))
    return value / 1000.0 if m.group(2).lower() == "ms" else value


def _resets_in_seconds(m: "re.Match[str]") -> float | None:
    if not any(m.groups()):  # "resets in" with no unit-bearing number: not this grammar
        return None
    return float(m.group(1) or 0) * 3600 + float(m.group(2) or 0) * 60 + float(m.group(3) or 0)


# An explicit "retry after N s" wins over "resets in ..." (the credential pool's precedence):
# a body carrying both describes a short throttle inside a long quota window, and the
# shorter explicit wait is the one the provider actually asks for.
RETRY_DELAY_PATTERNS = (
    (_QUOTA_RESET_DELAY_RE, _quota_reset_seconds),
    (_RETRY_AFTER_SECONDS_RE, lambda m: float(m.group(1))),
    (_RESETS_IN_SECONDS_FIELD_RE, lambda m: float(m.group(1))),
    (_RESETS_IN_RE, _resets_in_seconds),
)


def format_reset_window(seconds: float) -> str:
    """``~9h`` / ``~45 min`` for chat copy naming when a quota window reopens (ceilinged)."""
    seconds = int(seconds)
    return f"~{-(-seconds // 3600)}h" if seconds >= 3600 else f"~{-(-seconds // 60)} min"


def reset_delay_from_message(message: str) -> float | None:
    """Seconds-until-reset parsed from free-text provider error messages, or None."""
    if not message:
        return None
    for pattern, to_seconds in RETRY_DELAY_PATTERNS:
        m = pattern.search(message)
        if m and (seconds := to_seconds(m)) is not None:
            return seconds
    return None


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
