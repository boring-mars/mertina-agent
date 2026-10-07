# Ported from hermes-agent hermes_constants.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Shared constants for Mertina Agent.

Import-safe, stdlib-only — importable from anywhere without circular-import risk.
"""

import contextlib
import os
import sys
from contextvars import ContextVar
from pathlib import Path

_profile_fallback_warned: bool = False
_UNSET = object()
_MERTINA_HOME_OVERRIDE: ContextVar[str | object] = ContextVar(
    "_MERTINA_HOME_OVERRIDE", default=_UNSET
)


def get_mertina_home_override() -> str | None:
    """Return the active context-local Mertina home override, if any."""
    override = _MERTINA_HOME_OVERRIDE.get()
    return str(override) if override is not _UNSET and override else None


def _expand_mertina_home(path: str) -> Path:
    """Expand environment and user-home syntax in a Mertina home path."""
    return Path(os.path.expanduser(os.path.expandvars(path)))


def _get_platform_default_mertina_home() -> Path:
    """Return the platform-native default Mertina home path."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "mertina"
    return Path.home() / ".mertina"


def _warn_profile_fallback_once() -> None:
    """Warn once when MERTINA_HOME is unset but a non-default profile is sticky-active (wrong fallback)."""
    global _profile_fallback_warned
    if _profile_fallback_warned:
        return
    try:
        fallback_home = _get_platform_default_mertina_home()
        active_path = fallback_home / "active_profile"
        active = active_path.read_text(encoding="utf-8").strip() if active_path.exists() else ""
    except (UnicodeDecodeError, OSError):
        active = ""
    if active and active != "default":
        _profile_fallback_warned = True
        # Direct stderr, not logging: runs at import time (often before logging is
        # configured) and root-logger propagation would double-emit.
        msg = (
            f"[MERTINA_HOME fallback] MERTINA_HOME is unset but active "
            f"profile is {active!r}. Falling back to {fallback_home}, which "
            f"is the DEFAULT profile — not {active!r}. Any data this "
            f"process writes will land in the wrong profile. The "
            f"subprocess spawner should pass MERTINA_HOME explicitly "
            f"(see issue #18594)."
        )
        with contextlib.suppress(Exception):
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()


def get_mertina_home() -> Path:
    """Mertina home: context-local override → ``MERTINA_HOME`` env var → platform default."""
    override = get_mertina_home_override()
    if override:
        return _expand_mertina_home(override)
    if not os.environ.get("MERTINA_HOME", "").strip():
        _warn_profile_fallback_once()
    return get_process_mertina_home()


def get_process_mertina_home() -> Path:
    """Mertina home of the running process, ignoring task overrides.

    For process-level assets (theme YAML, dashboard plugin manifests) that must stay visible while a
    request is scoped to another profile (e.g. embedded ``/chat`` under ``--open-profile``).
    """
    val = os.environ.get("MERTINA_HOME", "").strip()
    return _expand_mertina_home(val) if val else _get_platform_default_mertina_home()


def get_config_path() -> Path:
    """Return the path to ``config.yaml`` under MERTINA_HOME."""
    return get_mertina_home() / "config.yaml"


PARTIAL_STREAM_STUB_ID = "partial-stream-stub"
FINISH_REASON_LENGTH = "length"
