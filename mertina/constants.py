# Ported from hermes-agent hermes_constants.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Shared constants for Mertina Agent.

Import-safe, stdlib-only — importable from anywhere without circular-import risk.
"""

import os
import sys
from pathlib import Path


def _expand_mertina_home(path: str) -> Path:
    """Expand environment and user-home syntax in a Mertina home path."""
    return Path(os.path.expanduser(os.path.expandvars(path)))  # noqa: PTH111  # upstream's call


def _get_platform_default_mertina_home() -> Path:
    """Return the platform-native default Mertina home path."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "mertina"
    return Path.home() / ".mertina"  # type: ignore[unreachable,unused-ignore]  # per-OS branch


def get_mertina_home() -> Path:
    """Mertina home: ``MERTINA_HOME`` env var → platform default."""
    return get_process_mertina_home()


# Resolved keys, keyed by the path string that was handed in. Path.resolve()
# is a filesystem call, and this function sits under every ToolRegistry
# lookup through current_scope_key(), so without this the registry pays a
# syscall per lookup. A process only ever sees a handful of home paths, so
# the dict stays tiny. Only paths that really exist are stored, see below.
_HOME_KEY_CACHE: dict[str, str] = {}


def mertina_home_key(path: str | Path | None = None) -> str:
    """Stable registry key for a Mertina home/profile dir.

    ``strict=False`` so profiles whose directories don't exist yet still get a key.

    The resolved value is remembered per input path. A directory that does
    not exist yet is resolved without touching the cache, because the answer
    can change once it is created (e.g. part of the path turns out to be a symlink).
    """
    candidate = Path(path) if path is not None else get_mertina_home()
    raw = str(candidate)
    cached = _HOME_KEY_CACHE.get(raw)
    if cached is not None:
        return cached
    expanded = candidate.expanduser()
    try:
        resolved = expanded.resolve(strict=True)
    except OSError:
        # Not on disk yet: lenient resolve, not stored, so the real answer is
        # picked up once the directory appears.
        return os.path.normcase(str(expanded.resolve(strict=False)))
    key = os.path.normcase(str(resolved))
    _HOME_KEY_CACHE[raw] = key
    return key


def get_process_mertina_home() -> Path:
    """Mertina home of the running process: ``MERTINA_HOME``, else the platform default."""
    val = os.environ.get("MERTINA_HOME", "").strip()
    return _expand_mertina_home(val) if val else _get_platform_default_mertina_home()


def get_config_path() -> Path:
    """Return the path to ``config.yaml`` under MERTINA_HOME."""
    return get_mertina_home() / "config.yaml"


PARTIAL_STREAM_STUB_ID = "partial-stream-stub"
FINISH_REASON_LENGTH = "length"
