# Ported from hermes-agent utils.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Shared utility functions for mertina-agent."""

import errno
import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import ParseResult, urlparse

import yaml

logger = logging.getLogger(__name__)


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


_IS_WINDOWS = os.name == "nt"


# Windows rename failures possibly caused by another handle on the target. CPython opens files
# without FILE_SHARE_DELETE, so ``os.replace`` onto an open file is denied with 5
# ERROR_ACCESS_DENIED (what a held *target* handle actually reports — measured: a plain reader
# yields 5, NOT 32), 32 ERROR_SHARING_VIOLATION (the *source* temp file is held) or 33
# ERROR_LOCK_VIOLATION (byte-range lock on the target). Ambiguous (a real ACL denial is also 5), so
# recovery is bounded and a still-failing write is re-raised unchanged rather than classified up
# front.
_WINDOWS_CONTENDED_REPLACE_ERRORS = frozenset({5, 32, 33})


# Retry budget for the atomic rename. A rename that wins here keeps the write fully atomic, so the
# budget covers a realistic hold (desktop auth-init holds auth.json >100 ms): ~200 ms recovered
# atomically, ~310 ms worst case. The cap matters as much as the count — gateway_state.json is
# rewritten every turn, so a permanently-held target pays the full budget per write. Jittered so
# concurrent writers don't retry in lockstep.
_REPLACE_RETRY_ATTEMPTS = 4


_REPLACE_RETRY_BASE_DELAY_S = 0.02


_REPLACE_RETRY_MAX_DELAY_S = 0.1


_CROSS_DEVICE_ERRNOS = (errno.EXDEV, errno.EBUSY)


def _is_contended_windows_replace_error(exc: OSError) -> bool:
    """Candidate-only: winerror 5 also covers a genuine ACL denial."""
    return _IS_WINDOWS and getattr(exc, "winerror", None) in _WINDOWS_CONTENDED_REPLACE_ERRORS


def _rewrite_in_place(tmp_str: str, real_path: str) -> None:
    """Overwrite *real_path* through the existing file — last resort for a still-held target.

    Not atomic (a smaller window than a copy, not none), so it runs only after the rename has
    genuinely failed. Writing through the target also preserves its ACL, which ``os.replace``
    does not (the temp file's inherited ACL wins there).
    """
    with open(tmp_str, "rb") as src:  # noqa: PTH123  # upstream's open()
        data = src.read()
    fd = os.open(real_path, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.ftruncate(fd, len(data))
        with suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)
    os.unlink(tmp_str)  # noqa: PTH108  # upstream's os.unlink()


def _copy_fallback(tmp_str: str, real_path: str) -> None:
    """Copy/fsync/unlink fallback for cross-device and bind-mount renames."""
    shutil.copyfile(tmp_str, real_path)
    with suppress(OSError):
        shutil.copystat(tmp_str, real_path)
    with suppress(OSError), open(real_path, "rb") as f:  # noqa: PTH123  # upstream's open()
        os.fsync(f.fileno())
    os.unlink(tmp_str)  # noqa: PTH108  # upstream's os.unlink()


def atomic_replace(tmp_path: str | Path, target: str | Path) -> str:
    """Atomically move *tmp_path* onto *target*, preserving symlinks.

    Resolves a symlink first so ``os.replace`` writes the real file in place and the symlink
    survives. Otherwise identical to ``os.replace`` unless the rename fails with EXDEV/EBUSY
    (cross-device, bind-mount, busy file: copy/fsync/unlink immediately — these never clear on
    retry) or a Windows rename contended by another open handle (winerror 5/32/33: bounded retry,
    then in-place rewrite).
    """
    target_str = str(target)
    real_path = os.path.realpath(target_str) if os.path.islink(target_str) else target_str  # noqa: PTH114  # upstream's os.path
    tmp_str = str(tmp_path)
    try:
        os.replace(tmp_str, real_path)  # noqa: PTH105  # upstream's os.replace()
        return real_path
    except OSError as exc:
        contended = _is_contended_windows_replace_error(exc)
        if exc.errno not in _CROSS_DEVICE_ERRNOS and not contended:
            raise
        if contended:
            # Lazy: keeps ``utils`` free of a package-level dependency on ``agent``.
            from mertina.agent.retry_utils import jittered_backoff

            for attempt in range(1, _REPLACE_RETRY_ATTEMPTS + 1):
                time.sleep(
                    jittered_backoff(
                        attempt,
                        base_delay=_REPLACE_RETRY_BASE_DELAY_S,
                        max_delay=_REPLACE_RETRY_MAX_DELAY_S,
                    )
                )
                try:
                    os.replace(tmp_str, real_path)  # noqa: PTH105  # upstream's os.replace()
                    return real_path
                except OSError as retry_exc:
                    exc = retry_exc
                    if retry_exc.errno in _CROSS_DEVICE_ERRNOS:
                        contended = False  # not contention after all — stop burning the budget
                        break
                    if not _is_contended_windows_replace_error(retry_exc):
                        raise
        logger.debug(
            "atomic_replace: %s -> %s failed with %s; falling back to %s",
            tmp_str,
            real_path,
            getattr(exc, "winerror", None) or errno.errorcode.get(exc.errno or 0, exc.errno),
            "in-place rewrite" if contended else "copy",
        )
        # The rewrite re-raises its own error, so an ACL denial is reported as such, not as
        # contention.
        (_rewrite_in_place if contended else _copy_fallback)(tmp_str, real_path)
    return real_path


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
