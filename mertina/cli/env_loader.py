# Ported from hermes-agent hermes_cli/env_loader.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Helpers for loading Mertina .env files consistently across entrypoints."""

from __future__ import annotations

import codecs
import io
import itertools
import logging
import os
import sys
import threading
from pathlib import Path

# Kept at module level on purpose: importing this module must fail when the dotenv install is wiped
# (#57828) so early recovery provably runs before third-party imports (test_early_recovery). The
# parser internals are imported lazily below because gateway tests stub ``sys.modules["dotenv"]``.
import dotenv  # noqa: F401

from mertina.utils import atomic_replace

logger = logging.getLogger(__name__)

# The ONLY env vars sanitized on load: credentials must be pure ASCII (they become HTTP header
# values); arbitrary user env vars are never silently altered.
_CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY")

# Once-per-process guards: load_mertina_dotenv() may run repeatedly (tests) so warnings fire once
# per key/path.
_WARNED_KEYS: set[str] = set()  # credential names already given the non-ASCII warning
_WARNED_UTF32_PATHS: set[str] = (
    set()
)  # .env paths already given the UTF-32 refuse-to-mangle warning

# What THIS process has published from dotenv files, per variable: (value before our first publish,
# or None if absent; last value we published; load pass that published it). Reloads run per gateway
# turn and per cron fire, and a line like ``PATH=/x:${PATH}`` interpolated against an environ that
# already holds the previous reload's output grows by ``/x:`` every time until child spawns fail
# with E2BIG (#109902). A new pass resolves against the baseline instead — but only where the
# environ still holds exactly what we published, so a value the shell changed since is not frozen.
# One process-wide record (not per home/project scope): the environ is process-wide, so alternating
# callers (gateway with a project .env, cron without; home A then B) must peel each other's output
# too.
_DOTENV_PUBLISHED: dict[str, tuple[str | None, str, int]] = {}
_DOTENV_PASSES = itertools.count()
_DOTENV_LOCK = threading.RLock()


def _format_offending_chars(value: str, limit: int = 3) -> str:
    """Compact ``U+XXXX ('c'), ...`` summary of non-ASCII codepoints."""
    seen: list[str] = []
    for ch in value:
        if ord(ch) > 127:
            label = f"U+{ord(ch):04X}"
            if ch.isprintable():
                label += f" ({ch!r})"
            if label not in seen:
                seen.append(label)
            if len(seen) >= limit:
                break
    return ", ".join(seen)


def _sanitize_loaded_credentials() -> None:
    """Strip non-ASCII from credential env vars (``_CREDENTIAL_SUFFIXES``) so the codebase never
    sees them.

    Emits a one-line warning to stderr when characters are stripped. Silent stripping would mask
    copy-paste corruption (Unicode lookalike glyphs from PDFs / rich-text editors, ZWSP from web
    pages) as opaque provider-side "invalid API key" errors (see #6843)."""
    for key, value in list(os.environ.items()):
        if not any(key.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES):
            continue
        if value.isascii():
            continue
        cleaned = value.encode("ascii", errors="ignore").decode("ascii")
        os.environ[key] = cleaned
        if key in _WARNED_KEYS:
            continue
        _WARNED_KEYS.add(key)
        stripped = len(value) - len(cleaned)
        detail = _format_offending_chars(value) or "non-printable"
        print(
            f"  Warning: {key} contained {stripped} non-ASCII character"
            f"{'s' if stripped != 1 else ''} ({detail}) — stripped so the "
            f"key can be sent as an HTTP header.",
            file=sys.stderr,
        )
        print(
            "  This usually means the key was copy-pasted from a PDF, "
            "rich-text editor, or web page that substituted lookalike\n"
            "  Unicode glyphs for ASCII letters. If authentication fails "
            '(e.g. "API key not valid"), re-copy the key from the\n'
            "  provider's dashboard into the .env file (edit it in a "
            "plain-text editor).",
            file=sys.stderr,
        )


def _load_dotenv_with_fallback(path: Path, *, override: bool, load_pass: int | None = None) -> None:
    """Load one dotenv file into ``os.environ`` like ``dotenv.load_dotenv`` — same parser, same
    ``${VAR}`` / ``${VAR:-default}`` / precedence rules — except that ``${VAR}`` resolves against
    the value VAR had before this process's earlier passes published it (see ``_DOTENV_PUBLISHED``).

    ``load_pass`` groups the layered files of one ``load_mertina_dotenv`` call: within a pass a
    later layer (project, managed) still sees the earlier layer's output, as it always did; only
    OTHER passes' output is peeled. A bare call (``mertina send``'s direct reload) is its own pass.
    """
    raw = path.read_bytes()
    try:
        # utf-8-sig strips a leading BOM (PowerShell 5.1 / Notepad); plain utf-8 would keep U+FEFF
        # on the first key name and silently drop it from os.environ under its canonical name.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        if raw.startswith(
            codecs.BOM_UTF8
        ):  # strip the BOM by hand: utf-8-sig can't once we decode latin-1
            raw = raw[len(codecs.BOM_UTF8) :]
        text = raw.decode("latin-1")
    # Imported here, not at module level: gateway tests stub ``sys.modules["dotenv"]`` with a bare
    # module exposing only ``load_dotenv``, and ``gateway.run`` imports this module at import time.
    from dotenv.main import DotEnv
    from dotenv.variables import parse_variables

    assignments = list(
        DotEnv(dotenv_path=None, stream=io.StringIO(text), interpolate=False).parse()
    )

    with _DOTENV_LOCK:
        if load_pass is None:
            load_pass = next(_DOTENV_PASSES)
        lookup_env: dict[str, str | None] = dict(os.environ)
        for name, (baseline, published, published_pass) in _DOTENV_PUBLISHED.items():
            if published_pass != load_pass and lookup_env.get(name) == published:
                if baseline is None:
                    del lookup_env[name]  # absent, so ``${VAR:-default}`` takes the default again
                else:
                    lookup_env[name] = baseline
        resolved: dict[str, str | None] = {}
        for name, value in assignments:
            if (
                value is not None
            ):  # mirrors dotenv.main.resolve_variables, minus the live os.environ
                lookup = {**lookup_env, **resolved} if override else {**resolved, **lookup_env}
                value = "".join(atom.resolve(lookup) for atom in parse_variables(value))
            resolved[name] = value
        for name, value in resolved.items():
            if value is None or (not override and name in os.environ):
                continue
            current = os.environ.get(name)
            record = _DOTENV_PUBLISHED.get(name)
            ours = record is not None and current == record[1]
            # Ours and untouched since → keep the original baseline; anything else is a newer
            # outside value.
            baseline = record[0] if ours else current  # type: ignore[index]  # ours implies a record
            os.environ[name] = value
            _DOTENV_PUBLISHED[name] = (baseline, value, load_pass)
    _sanitize_loaded_credentials()  # httpx encodes headers as ASCII


def _sanitize_env_file_if_needed(path: Path) -> None:
    """Pre-sanitize a .env file before python-dotenv reads it. Sniffs a leading BOM *before* any
    text decode: UTF-16 (Notepad "Unicode") is rewritten as clean UTF-8; UTF-32 is refused (left
    untouched) so we never fall through to the errors=replace corruption path."""
    if not path.exists():
        return
    try:
        from mertina.cli.config import _sanitize_env_lines
    except ImportError:
        return  # early bootstrap — config module not available yet

    try:
        raw = path.read_bytes()
    except Exception:
        return

    # ORDER MATTERS: BOM_UTF32_LE (FF FE 00 00) startswith BOM_UTF16_LE (FF FE); UTF-16 first would
    # mangle it.
    force_utf8_rewrite = False
    if raw.startswith(codecs.BOM_UTF32_LE) or raw.startswith(codecs.BOM_UTF32_BE):
        # Lazy import keeps the module import block identical to #65124's codecs/io additions so the
        # two PRs auto-merge either order.
        path_key = str(path.resolve())
        if path_key not in _WARNED_UTF32_PATHS:
            _WARNED_UTF32_PATHS.add(path_key)
            logger.warning(
                "Skipping .env sanitize for %s: UTF-32 BOM detected; "
                "leaving file untouched to avoid corruption",
                path,
            )
        return
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        # "utf-16" uses the BOM for endianness and strips it; newline=None matches open()'s
        # universal newlines (not splitlines()'s extra boundaries like U+2028) so sanitize sees the
        # same lines.
        try:
            with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-16", newline=None) as f:
                original = f.readlines()
        except UnicodeDecodeError:
            return
        force_utf8_rewrite = (
            True  # always rewrite UTF-16 as UTF-8 so the dotenv load sees a canonical file
        )
    else:
        # utf-8-sig strips a UTF-8 BOM; errors=replace so embedded NULs can be stripped below.
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:  # noqa: PTH123  # upstream's open()
                original = f.readlines()
        except Exception:
            return
        # errors=replace turns undecodable leading bytes into U+FFFD; persisting would glue them
        # onto the first key name permanently — leave the file untouched instead.
        if original and original[0].startswith("\ufffd"):
            return

    try:
        # Strip NULs (os.environ raises ValueError on them); also repairs BOM-less UTF-16
        # (NUL-padded ASCII).
        stripped = [line.replace("\x00", "") for line in original]
        sanitized = _sanitize_env_lines(stripped)
        if sanitized != original or force_utf8_rewrite:
            import tempfile

            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".env_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(sanitized)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)  # noqa: PTH108  # upstream's os.unlink()
                except OSError:
                    pass
                raise
    except Exception:
        pass  # best-effort — don't block gateway startup


def load_mertina_dotenv(
    *,
    mertina_home: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Load the Mertina env file: ``~/.mertina/.env`` overrides stale shell exports."""
    # Process home on purpose (never the per-turn override): a startup .env load must not follow a
    # routed profile.
    from mertina.constants import get_process_mertina_home

    home_path = Path(mertina_home) if mertina_home else get_process_mertina_home()

    loaded: list[Path] = []
    user_env = home_path / ".env"
    load_pass = next(_DOTENV_PASSES)

    if user_env.exists():  # normalize formatting / strip NULs before parsing
        _sanitize_env_file_if_needed(user_env)

    if user_env.exists():
        _load_dotenv_with_fallback(user_env, override=True, load_pass=load_pass)
        loaded.append(user_env)
    return loaded
