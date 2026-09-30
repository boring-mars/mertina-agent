# Ported from hermes-agent hermes_time.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Timezone-aware clock for Mertina.

``now()`` returns a tz-aware datetime in the IANA timezone named by the ``MERTINA_TIMEZONE`` env
var, else server-local time. Invalid timezone values log a warning and fall back — never crash.
"""

import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Cache keyed by timezone *source* identity. Entries are published atomically under
# ``_cache_lock`` as one ``identity -> (name, ZoneInfo | None)`` value, so racing resolvers can
# never publish a mixed identity/value pair.
_cache_lock = threading.Lock()
_tz_cache: dict[tuple[str, str], tuple[str, ZoneInfo | None]] = {}


def _env_timezone() -> str:
    """``MERTINA_TIMEZONE``, stripped."""
    return os.getenv("MERTINA_TIMEZONE", "").strip()


def _timezone_cache_identity() -> tuple[str, str]:
    tz_env = _env_timezone()
    return ("environment", tz_env)


def _resolve_timezone_name() -> str:
    """Read the configured IANA timezone string (or ``""``)."""
    tz_env = _env_timezone()
    if tz_env:
        return tz_env
    return ""


def _timezone_entry() -> tuple[str, ZoneInfo | None]:
    """Cached ``(configured name, ZoneInfo | None)`` for the active profile."""
    cache_identity = _timezone_cache_identity()
    with _cache_lock:
        entry = _tz_cache.get(cache_identity)
        if entry is not None:
            return entry
    # Resolve outside the lock; first writer wins so concurrent resolvers of the
    # same identity converge on one ZoneInfo object.
    name = _resolve_timezone_name()
    tz = None
    if name:
        try:
            tz = ZoneInfo(name)
        except Exception as exc:
            logger.warning(
                "Invalid timezone '%s': %s. Falling back to server local time.", name, exc
            )
    with _cache_lock:
        return _tz_cache.setdefault(cache_identity, (name, tz))


def get_timezone() -> ZoneInfo | None:
    """Return the active profile's configured ZoneInfo, or None (server-local)."""
    return _timezone_entry()[1]


def now() -> datetime:
    """Current time as a tz-aware datetime: configured zone, else server-local."""
    tz = get_timezone()
    return datetime.now(tz) if tz is not None else datetime.now().astimezone()
