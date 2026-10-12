"""Shared utility functions for hermes-agent."""

import errno
import json
import logging
import os
import shutil
import stat
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Union
from urllib.parse import urlparse

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


def _preserve_file_mode(path: Path) -> "int | None":
    """Permission bits of *path* if it exists, else ``None``."""
    try:
        return stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    except OSError:
        return None


def _preserve_file_owner(path: Path) -> "tuple[int, int] | None":
    """Owning ``(uid, gid)`` of *path* on POSIX, else ``None``."""
    try:
        st = path.stat() if os.name == "posix" else None
    except OSError:
        return None
    return (st.st_uid, st.st_gid) if st else None


def _restore_file_metadata(path: Path, owner: "tuple[int, int] | None", mode: "int | None") -> None:
    """Best-effort re-apply of uid/gid and permission bits after an atomic replace.

    Docker/NAS installs often run some commands as root on a volume owned by the runtime user;
    ``os.replace`` swaps in the temp file's owner, so privileged callers chown it back. ``mkstemp``
    creates 0o600 files; without re-applying *mode* the target would inherit that and break
    volume mounts relying on broader permissions.
    """
    if owner is not None and hasattr(os, "chown"):
        with suppress(OSError):
            os.chown(path, owner[0], owner[1])
    if mode is not None:
        with suppress(OSError):
            os.chmod(path, mode)


def default_new_file_mode() -> "int | None":
    """The mode ``open(path, "w")`` gives a file it has to create (``0o666 & ~umask``); ``None``
    when the umask cannot be read or on non-POSIX hosts (Windows mode bits are synthesized).

    ``mkstemp`` always creates at 0600, so publishing a *new* non-secret file through a temp
    file would tighten it to owner-only — the Docker/NAS volume-mount hazard
    :func:`_restore_file_metadata` documents. The transient mask is 0o077: a thread that opens
    a file in the read window gets a tighter file, never a looser one.
    """
    if os.name != "posix":
        return None
    try:
        current = os.umask(0o077)
        os.umask(current)
    except OSError:
        return None
    return 0o666 & ~current






_IS_WINDOWS = os.name == "nt"
# Windows rename failures possibly caused by another handle on the target. CPython opens files
# without FILE_SHARE_DELETE, so ``os.replace`` onto an open file is denied with 5 ERROR_ACCESS_DENIED
# (what a held *target* handle actually reports — measured: a plain reader yields 5, NOT 32),
# 32 ERROR_SHARING_VIOLATION (the *source* temp file is held) or 33 ERROR_LOCK_VIOLATION (byte-range
# lock on the target). Ambiguous (a real ACL denial is also 5), so recovery is bounded and a
# still-failing write is re-raised unchanged rather than classified up front.
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
    with open(tmp_str, "rb") as src:
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
    os.unlink(tmp_str)


def _copy_fallback(tmp_str: str, real_path: str) -> None:
    """Copy/fsync/unlink fallback for cross-device and bind-mount renames."""
    shutil.copyfile(tmp_str, real_path)
    with suppress(OSError):
        shutil.copystat(tmp_str, real_path)
    with suppress(OSError), open(real_path, "rb") as f:
        os.fsync(f.fileno())
    os.unlink(tmp_str)


def atomic_replace(tmp_path: Union[str, Path], target: Union[str, Path]) -> str:
    """Atomically move *tmp_path* onto *target*, preserving symlinks.

    Resolves a symlink first so ``os.replace`` writes the real file in place and the symlink
    survives. Otherwise identical to ``os.replace`` unless the rename fails with EXDEV/EBUSY
    (cross-device, bind-mount, busy file: copy/fsync/unlink immediately — these never clear on
    retry) or a Windows rename contended by another open handle (winerror 5/32/33: bounded retry,
    then in-place rewrite).
    """
    target_str = str(target)
    real_path = os.path.realpath(target_str) if os.path.islink(target_str) else target_str
    tmp_str = str(tmp_path)
    try:
        os.replace(tmp_str, real_path)
        return real_path
    except OSError as exc:
        contended = _is_contended_windows_replace_error(exc)
        if exc.errno not in _CROSS_DEVICE_ERRNOS and not contended:
            raise
        if contended:
            # Lazy: keeps ``utils`` free of a package-level dependency on ``agent``.
            from agent.retry_utils import jittered_backoff
            for attempt in range(1, _REPLACE_RETRY_ATTEMPTS + 1):
                time.sleep(jittered_backoff(attempt, base_delay=_REPLACE_RETRY_BASE_DELAY_S, max_delay=_REPLACE_RETRY_MAX_DELAY_S))
                try:
                    os.replace(tmp_str, real_path)
                    return real_path
                except OSError as retry_exc:
                    exc = retry_exc
                    if retry_exc.errno in _CROSS_DEVICE_ERRNOS:
                        contended = False  # not contention after all — stop burning the budget
                        break
                    if not _is_contended_windows_replace_error(retry_exc):
                        raise
        logger.debug("atomic_replace: %s -> %s failed with %s; falling back to %s", tmp_str, real_path,
                     getattr(exc, "winerror", None) or errno.errorcode.get(exc.errno or 0, exc.errno),
                     "in-place rewrite" if contended else "copy")
        # The rewrite re-raises its own error, so an ACL denial is reported as such, not as contention.
        (_rewrite_in_place if contended else _copy_fallback)(tmp_str, real_path)
    return real_path


def fsync_directory(path: Union[str, Path]) -> None:
    """Best-effort fsync of a directory entry so a just-renamed file survives power loss.

    No-op on Windows (directories can't be opened with ``os.open``; the file fsync still applies)
    and on any OSError — durability of the directory entry is never worth failing a write that
    has already been replaced into place.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)




def _atomic_write(path: Path, write, *, prefix: str, encoding: str = "utf-8", mode: "int | None" = None,
                  preserve_owner: bool = True, binary: bool = False, fsync_dir: bool = False) -> None:
    """Temp file + fsync + :func:`atomic_replace`, then re-apply owner/mode.

    *write(f)* emits the payload into the open handle (text, or bytes when *binary*). The temp file
    is created by ``mkstemp`` — ``O_CREAT|O_EXCL`` at 0600 regardless of umask — so a secret is
    never readable at process umask, not even between create and chmod. *mode* is fchmod'd onto
    the temp fd BEFORE the replace so the target never transits through mkstemp's 0600 (fchmod is
    Unix-only; the post-replace chmod is the sole path on Windows). With no *mode* a NEW target
    gets what ``open(path, "w")`` would have given it (process umask) — the callers this replaced
    wrote at umask, and silently tightening every fresh cache/state file to 0600 breaks shared
    volume mounts; an existing target with no *mode* keeps mkstemp's bits, as before. *fsync_dir*
    also fsyncs the resolved target's parent so the rename itself is durable. The temp file is
    removed on any failure — ``BaseException`` on purpose, so KeyboardInterrupt / SystemExit still
    clean up.
    """
    # A profile delete leaves a tombstone beside its removed home.  Background
    # writers may retain that home in a context variable, so a plain mkdir here
    # would resurrect the profile before the write can fail.
    from hermes_constants import mkdir_under_hermes_home

    mkdir_under_hermes_home(path.parent)
    if mode is None and not path.exists():
        mode = default_new_file_mode()
    original_owner = _preserve_file_owner(path) if preserve_owner else None
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb" if binary else "w", encoding=None if binary else encoding) as f:
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), mode)
            write(f)
            f.flush()
            os.fsync(f.fileno())
        replaced = Path(atomic_replace(tmp_path, path))  # symlink-preserving actual destination
        _restore_file_metadata(replaced, original_owner, mode)
        if fsync_dir:
            fsync_directory(replaced.parent)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise








def _dump_json(data: Any, f, *, indent: "int | None", ensure_ascii: bool, dump_kwargs: dict) -> None:
    """``json.dump`` that survives surrogate-escaped strings.

    ``os.fsdecode`` of a non-UTF-8 filename/argv yields lone surrogates (``'\\udcff'``); a utf-8
    text handle rejects them with ``UnicodeEncodeError`` — a ValueError, which callers guarding
    ``except OSError`` never see. ``ensure_ascii=True`` escapes them as ``\\udcff`` and
    ``json.loads`` restores the identical str, so the retry round-trips; ``surrogateescape``
    would emit a raw 0xFF byte that the reader's utf-8 decode rejects. Serializing to a str first
    keeps the failure before any byte reaches the file, so no partial payload is left behind.
    """
    text = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii, **dump_kwargs)
    try:
        f.write(text)
    except UnicodeEncodeError:
        f.write(json.dumps(data, indent=indent, ensure_ascii=True, **dump_kwargs))


def atomic_json_write(
    path: Union[str, Path], data: Any, *, indent: int = 2, mode: int | None = None,
    ensure_ascii: bool = False, fsync_dir: bool = False, **dump_kwargs: Any,
) -> None:
    """Write JSON to *path* atomically (temp file + fsync + replace).

    Surrogate-escaped strings (non-UTF-8 argv/paths) are always persisted: the write falls back
    to ``ensure_ascii=True`` escapes for that payload only, so normal content keeps its raw UTF-8
    bytes. ``mode=0o600`` is the private-credential form: the temp file is 0600 from creation
    (mkstemp), so the payload is never umask-readable.
    """
    path = Path(path)
    _atomic_write(path, lambda f: _dump_json(data, f, indent=indent, ensure_ascii=ensure_ascii, dump_kwargs=dump_kwargs),
                  prefix=f".{path.stem}_", mode=mode if mode is not None else _preserve_file_mode(path),
                  fsync_dir=fsync_dir)
















# ruamel's round-trip dumper resolves plain scalars under YAML 1.2, where only true/false/null are
# reserved — so a str like "off" or "yes" is emitted unquoted. Every other config reader here
# (PyYAML, yaml.safe_load sites) parses under YAML 1.1, where on/off/yes/no are booleans: an
# unquoted ``approvals.mode: off`` would silently round-trip back as ``False``.




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


def _env_number(key: str, default, cast):
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










def _parse_base_url(base_url: str):
    """``urlparse`` that tolerates a bare ``host[:port][/path]`` (no scheme)."""
    raw = (base_url or "").strip()
    return urlparse(raw if "://" in raw else f"//{raw}") if raw else None


def _hostname_of(parsed) -> str:
    return (parsed.hostname or "").lower().rstrip(".") if parsed else ""


def base_url_hostname(base_url: str) -> str:
    """Lowercased hostname for a base URL, or ``""`` if absent.

    Compare exact hostnames against provider hosts instead of substring-matching the raw URL:
    ``https://api.openai.com.example/v1`` or ``https://proxy.test/api.openai.com/v1`` would
    otherwise pass as native endpoints and mis-route api_mode and auth.
    """
    return _hostname_of(_parse_base_url(base_url))




def model_forces_max_completion_tokens(model: str) -> bool:
    """True for OpenAI families that reject ``max_tokens`` (HTTP 400 ``unsupported_parameter``)."""
    m = (model or "").strip().lower().rsplit("/", 1)[-1]
    return m.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4"))




def base_url_host_matches(base_url: str, domain: str) -> bool:
    """True when the base URL's hostname is ``domain`` or a subdomain.

    Safer than ``domain in base_url`` (``evil.com/moonshot.ai`` / ``moonshot.ai.evil`` must not
    match). Accepts bare hosts, full URLs, and URLs with paths.
    """
    hostname = base_url_hostname(base_url)
    domain = (domain or "").strip().lower().rstrip(".")
    return bool(hostname and domain) and (hostname == domain or hostname.endswith("." + domain))
