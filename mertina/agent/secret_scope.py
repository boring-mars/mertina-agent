# Ported from hermes-agent agent/secret_scope.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""The ``.env`` tokenizer: :func:`load_env_file` parses a ``.env`` file into a dict without
touching ``os.environ``.
"""

from __future__ import annotations

import codecs
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

from mertina.utils import file_signature


def _strip_inline_comment(value: str) -> str:
    """Strip a dotenv-style inline comment (python-dotenv semantics): quoted values
    scan to the matching close quote (backslash-aware for double quotes) and drop a
    trailing ``# ...``, else stay untouched; unquoted values truncate only at a
    ``#`` PRECEDED BY WHITESPACE (``foo#bar`` survives, ``value # c`` → ``value``)."""
    value = value.strip()
    if not value:
        return value
    quote = value[0]
    if quote in ("'", '"'):
        i = 1
        while i < len(value):
            ch = value[i]
            if quote == '"' and ch == "\\":
                i += 2  # skip the escaped character
                continue
            if ch == quote:
                return value[: i + 1] if value[i + 1 :].lstrip().startswith("#") else value
            i += 1
        return value  # unterminated quote: leave as-is
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _parse_env_value(raw_value: str) -> str:
    """Parse the small .env value subset Mertina writes itself (bare, 'single', or "double" with
    ``\\"`` / ``\\\\`` escapes)."""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        quoted = value[1:-1]
        parsed: list[str] = []
        i = 0
        while i < len(quoted):
            escaped = quoted[i] == "\\" and quoted[i + 1 : i + 2] in ('"', "\\")
            parsed.append(quoted[i + 1] if escaped else quoted[i])
            i += 2 if escaped else 1
        return "".join(parsed)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


# Per-path memo of parsed ``.env`` files: ``get_env_value()`` reads the file on every lookup.
#
# FRESHNESS: every call still OPENS the file and keys on ``utils.file_signature`` of that
# descriptor's ``fstat`` (mtime_ns, size, inode, ctime_ns — ctime can't be backdated, so a pinned-
# timestamp rewrite is still seen), re-checked after the read. The open keeps close-to-open
# revalidation on NFS, a vanished/unreadable file fails the open and is never cached (a transient
# EACCES must not become "this profile has no secrets"), and the descriptor pins one inode so a
# symlink repointed mid-read can't file one file's contents under another's identity.
# ``invalidate_env_file_cache()`` is the explicit knob.
_ENV_FILE_CACHE: OrderedDict[str, tuple[tuple, dict[str, str]]] = OrderedDict()
_ENV_FILE_CACHE_LOCK = threading.Lock()
_ENV_FILE_CACHE_MAX = 64


def invalidate_env_file_cache(env_path: Path | None = None) -> None:
    """Drop one path from the ``load_env_file()`` memo, or all of them."""
    with _ENV_FILE_CACHE_LOCK:
        if env_path is None:
            _ENV_FILE_CACHE.clear()
        else:
            _ENV_FILE_CACHE.pop(str(env_path), None)


def _decode_env_bytes(raw: bytes) -> str:
    """BOM stripped; invalid UTF-8 falls back to latin-1."""
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8) :]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _parse_env_text(text: str) -> dict[str, str]:
    """Tokenize already-read ``.env`` text. See :func:`load_env_file`."""
    secrets: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key:
            secrets[key] = _parse_env_value(_strip_inline_comment(value))
    return secrets


def load_env_file(env_path: Path) -> dict[str, str]:
    """THE ``.env`` tokenizer (``mertina.cli.config.load_env`` reads through here). Dict only —
    never touches ``os.environ``. ``export`` prefix, ``#`` comments, quote escapes reversed; a BOM
    is stripped so it doesn't prefix the first key. Invalid UTF-8 decodes as latin-1. Absent or
    unreadable → ``{}``.

    Memoised per path on the open descriptor's stat identity (see the cache comment above). Always
    returns a fresh dict: callers may mutate what they get back.
    """
    key = str(env_path)
    try:
        with open(env_path, "rb") as handle:  # noqa: PTH123  # upstream's open()
            fingerprint = file_signature(os.fstat(handle.fileno()))
            with _ENV_FILE_CACHE_LOCK:
                cached = _ENV_FILE_CACHE.get(key)
                if cached is not None and cached[0] == fingerprint:
                    _ENV_FILE_CACHE.move_to_end(key)
                    return dict(cached[1])
            raw = handle.read()
            # Same descriptor: a rewrite that landed between the fstat and the read is parsed but
            # not stored under the pre-write fingerprint.
            settled = file_signature(os.fstat(handle.fileno())) == fingerprint
    except OSError:
        # Gone or unreadable: drop any entry so a stale map cannot outlive the file.
        invalidate_env_file_cache(env_path)
        return {}

    secrets = _parse_env_text(_decode_env_bytes(raw))
    if settled:
        with _ENV_FILE_CACHE_LOCK:
            _ENV_FILE_CACHE[key] = (fingerprint, dict(secrets))
            _ENV_FILE_CACHE.move_to_end(key)
            while len(_ENV_FILE_CACHE) > _ENV_FILE_CACHE_MAX:
                _ENV_FILE_CACHE.popitem(last=False)
    return secrets
