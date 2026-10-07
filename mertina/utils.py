# Ported from hermes-agent utils.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Shared utility functions for mertina-agent."""

import json
import os
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import ParseResult, urlparse

import yaml

TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on"})


def is_truthy_value(value: Any, default: bool = False) -> bool:
    """Coerce bool-ish values using the project's shared truthy string set."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_STRINGS
    return bool(value)


def env_var_enabled(name: str, default: str = "") -> bool:
    """Return True when an environment variable is set to a truthy value."""
    return is_truthy_value(os.getenv(name, default), default=False)


def file_signature(st: os.stat_result) -> "tuple[int, int, int, int]":
    """Change-detection key for a stat result: ``(st_mtime_ns, st_size, st_ino, st_ctime_ns)``.

    mtime + size alone miss a replacement that preserves both (``cp -p``, ``rsync -t``, a tar
    restore, a script pinning the timestamp with ``os.utime``). The inode changes on an atomic
    replace and ctime cannot be backdated from user space, so the pair catches those writers.
    On Windows ``st_ino`` may be 0 and ``st_ctime_ns`` is the creation time — both stable across
    an in-place rewrite, so the key degrades to mtime + size there rather than misfiring.
    """
    return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)


def safe_json_loads(text: str, default: Any = None) -> Any:
    """Parse JSON, returning *default* on any parse error."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


# libyaml's CSafeLoader is ~8x faster than the pure-Python SafeLoader and a true drop-in for
# ``safe_load`` (same restricted tag set); startup parses config.yaml and every plugin manifest,
# so the slow path cost ~0.9 s of cold start.
_fast_yaml_loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader


def fast_safe_load(stream: Any) -> Any:
    """``yaml.safe_load`` (same inputs, same result) using the libyaml C loader when available."""
    return yaml.load(stream, Loader=_fast_yaml_loader)


_N = TypeVar("_N", int, float)


def _env_number(key: str, default: _N, cast: Callable[[str], _N]) -> _N:
    raw = os.getenv(key, "").strip()
    try:
        return cast(raw) if raw else default
    except (ValueError, TypeError):
        return default


def env_int(key: str, default: int = 0) -> int:
    """Read an environment variable as an integer, with fallback."""
    return _env_number(key, default, int)


def env_float(key: str, default: float = 0.0) -> float:
    """Read an environment variable as a float, with fallback."""
    return _env_number(key, default, float)


def _parse_base_url(base_url: str) -> ParseResult | None:
    """``urlparse`` that tolerates a bare ``host[:port][/path]`` (no scheme)."""
    raw = (base_url or "").strip()
    return urlparse(raw if "://" in raw else f"//{raw}") if raw else None


def _hostname_of(parsed: ParseResult | None) -> str:
    return (parsed.hostname or "").lower().rstrip(".") if parsed else ""


def base_url_hostname(base_url: str) -> str:
    """Lowercased hostname for a base URL, or ``""`` if absent.

    Compare exact hostnames against provider hosts instead of substring-matching the raw URL:
    ``https://api.openai.com.example/v1`` or ``https://proxy.test/api.openai.com/v1`` would
    otherwise pass as native endpoints and mis-route api_mode and auth.
    """
    return _hostname_of(_parse_base_url(base_url))


def base_url_host_matches(base_url: str, domain: str) -> bool:
    """True when the base URL's hostname is ``domain`` or a subdomain.

    Safer than ``domain in base_url`` (``evil.com/moonshot.ai`` / ``moonshot.ai.evil`` must not
    match). Accepts bare hosts, full URLs, and URLs with paths.
    """
    hostname = base_url_hostname(base_url)
    domain = (domain or "").strip().lower().rstrip(".")
    return bool(hostname and domain) and (hostname == domain or hostname.endswith("." + domain))
