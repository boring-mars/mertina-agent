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


def get_process_mertina_home() -> Path:
    """Mertina home of the running process: ``MERTINA_HOME``, else the platform default."""
    val = os.environ.get("MERTINA_HOME", "").strip()
    return _expand_mertina_home(val) if val else _get_platform_default_mertina_home()


def get_config_path() -> Path:
    """Return the path to ``config.yaml`` under MERTINA_HOME."""
    return get_mertina_home() / "config.yaml"


PARTIAL_STREAM_STUB_ID = "partial-stream-stub"
FINISH_REASON_LENGTH = "length"
