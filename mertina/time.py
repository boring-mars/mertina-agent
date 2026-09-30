# Ported from hermes-agent hermes_time.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Timezone-aware clock for Hermes.

``now()`` returns a tz-aware datetime in the user's configured IANA timezone. Resolution order:
``HERMES_TIMEZONE`` env var, then ``timezone`` in ``~/.hermes/config.yaml``, else server-local
time. Invalid timezone values log a warning and fall back — never crash.
"""

import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from mertina.constants import get_config_path

logger = logging.getLogger(__name__)

# Cache keyed by timezone *source* identity. This process can multiplex profiles by switching
# HERMES_HOME, so one unkeyed global would leak the first profile's timezone into later
# profile-scoped work (e.g. the desktop multiplex cron ticker persisting another profile's
# ``next_run_at``). Entries are published atomically under ``_cache_lock`` as one
# ``identity -> (name, ZoneInfo | None)`` value, so racing resolvers can never publish a mixed
# identity/value pair. Call reset_cache() after in-place config changes.
_cache_lock = threading.Lock()
_tz_cache: dict[tuple[str, str], tuple[str, ZoneInfo | None]] = {}


def _env_timezone() -> str:
    """``HERMES_TIMEZONE`` when it may speak for the active profile. Under the multiplexed
    gateway the env var holds only the DEFAULT profile's value (bridged from its config.yaml at
    startup), so every routed profile must read its own config.yaml instead."""
    from mertina.agent.secret_scope import (
        is_multiplex_active,  # lazy: secret_scope pulls in more than a clock needs
    )

    if is_multiplex_active():
        return ""
    return os.getenv("HERMES_TIMEZONE", "").strip()


def _timezone_cache_identity() -> tuple[str, str]:
    tz_env = _env_timezone()
    return ("environment", tz_env) if tz_env else ("config", str(get_config_path()))


def _resolve_timezone_name() -> str:
    """Read the configured IANA timezone string (or ``""``). Does file I/O — callers cache."""
    tz_env = _env_timezone()
    if tz_env:
        return tz_env
    try:
        # Prefer the shared cached effective-config loader (mtime-keyed + libyaml, managed overlay
        # included so an administrator can pin ``timezone``): a direct safe_load of a large
        # config.yaml costs ~100 ms and this ran inside the FIRST system prompt build. The bare
        # parse is the stdlib-safe fallback for bootstrap consumers without hermes_cli importable.
        try:
            from mertina.cli.config_effective import load_user_config_effective

            cfg = load_user_config_effective(get_config_path())
        except Exception:
            import yaml

            config_path = get_config_path()
            cfg = (
                (yaml.safe_load(config_path.read_text(encoding="utf-8")) or {})
                if config_path.exists()
                else {}
            )
        if cfg:
            tz_cfg = cfg.get("timezone", "")
            if isinstance(tz_cfg, str) and tz_cfg.strip():
                return tz_cfg.strip()
    except Exception:
        pass
    return ""


def _timezone_entry() -> tuple[str, ZoneInfo | None]:
    """Cached ``(configured name, ZoneInfo | None)`` for the active profile."""
    cache_identity = _timezone_cache_identity()
    with _cache_lock:
        entry = _tz_cache.get(cache_identity)
        if entry is not None:
            return entry
    # Resolve outside the lock (config file I/O); first writer wins so concurrent resolvers of the
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
