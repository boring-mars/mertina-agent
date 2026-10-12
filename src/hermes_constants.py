"""Shared constants for Hermes Agent.

Import-safe, stdlib-only — importable from anywhere without circular-import risk.
"""

import contextlib

import os

import re

import shutil

import stat

import sys

from collections.abc import MutableMapping

from contextvars import ContextVar, Token

from pathlib import Path

_profile_fallback_warned: bool = False

_UNSET = object()

_HERMES_HOME_OVERRIDE: ContextVar[str | object] = ContextVar("_HERMES_HOME_OVERRIDE", default=_UNSET)

def set_hermes_home_override(path: str | Path | None) -> Token:
    """Set a context-local Hermes home override and return its reset token.

    Deliberately does not mutate ``os.environ`` (shared by every thread in the process).
    """
    value: str | object = _UNSET if path is None else str(path)
    return _HERMES_HOME_OVERRIDE.set(value)

def reset_hermes_home_override(token: Token) -> None:
    """Restore the previous context-local Hermes home override."""
    _HERMES_HOME_OVERRIDE.reset(token)

def get_hermes_home_override() -> str | None:
    """Return the active context-local Hermes home override, if any."""
    override = _HERMES_HOME_OVERRIDE.get()
    return str(override) if override is not _UNSET and override else None

def _expand_hermes_home(path: str) -> Path:
    """Expand environment and user-home syntax in a Hermes home path."""
    return Path(os.path.expanduser(os.path.expandvars(path)))

def _get_platform_default_hermes_home() -> Path:
    """Return the platform-native default Hermes home path."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"

def _warn_profile_fallback_once() -> None:
    """Warn once when HERMES_HOME is unset but a non-default profile is sticky-active (wrong fallback)."""
    global _profile_fallback_warned
    if _profile_fallback_warned:
        return
    try:
        fallback_home = _get_platform_default_hermes_home()
        active_path = fallback_home / "active_profile"
        active = active_path.read_text(encoding="utf-8").strip() if active_path.exists() else ""
    except (UnicodeDecodeError, OSError):
        active = ""
    if active and active != "default":
        _profile_fallback_warned = True
        # Direct stderr, not logging: runs at import time (often before logging is
        # configured) and root-logger propagation would double-emit.
        msg = (
            f"[HERMES_HOME fallback] HERMES_HOME is unset but active "
            f"profile is {active!r}. Falling back to {fallback_home}, which "
            f"is the DEFAULT profile — not {active!r}. Any data this "
            f"process writes will land in the wrong profile. The "
            f"subprocess spawner should pass HERMES_HOME explicitly "
            f"(see issue #18594)."
        )
        with contextlib.suppress(Exception):
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()

def get_hermes_home() -> Path:
    """Hermes home: context-local override → ``HERMES_HOME`` env var → platform default."""
    override = get_hermes_home_override()
    if override:
        return _expand_hermes_home(override)
    if not os.environ.get("HERMES_HOME", "").strip():
        _warn_profile_fallback_once()
    return get_process_hermes_home()

_HOME_KEY_CACHE: dict[str, str] = {}

def hermes_home_key(path: str | Path | None = None) -> str:
    """Stable registry key for a Hermes home/profile dir.

    ``strict=False`` so profiles whose directories don't exist yet still get a key.

    The resolved value is remembered per input path. A directory that does
    not exist yet is resolved without touching the cache, because the answer
    can change once it is created (e.g. part of the path turns out to be a symlink).
    """
    candidate = Path(path) if path is not None else get_hermes_home()
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

def get_process_hermes_home() -> Path:
    """Hermes home of the running process, ignoring task overrides.

    For process-level assets (theme YAML, dashboard plugin manifests) that must stay visible while a
    request is scoped to another profile (e.g. embedded ``/chat`` under ``--open-profile``).
    """
    val = os.environ.get("HERMES_HOME", "").strip()
    return _expand_hermes_home(val) if val else _get_platform_default_hermes_home()

_default_hermes_root_memo: "tuple[str, str, Path] | None" = None

def get_default_hermes_root() -> Path:
    """Root Hermes dir for profile-level ops: ``<root>`` when ``HERMES_HOME=<root>/profiles/<name>``."""
    global _default_hermes_root_memo
    native_home = _get_platform_default_hermes_home()
    env_home = os.environ.get("HERMES_HOME", "").strip()
    env_path = _expand_hermes_home(env_home) if env_home else None
    memo_key = (str(native_home), str(env_path) if env_path is not None else "")
    memo = _default_hermes_root_memo
    if memo is not None and memo[:2] == memo_key:
        return memo[2]
    result = native_home
    if env_path is not None:
        try:
            env_path.resolve().relative_to(native_home.resolve())  # under ~/.hermes (normal or profile mode)
        except ValueError:  # Docker/custom root: <root>/profiles/<name> -> <root>, else HERMES_HOME itself
            result = env_path.parent.parent if env_path.parent.name == "profiles" else env_path
    _default_hermes_root_memo = (*memo_key, result)
    return result

_DELETED_PROFILES_DIR = ".deleted"

_HERMES_HOME_MARKERS = ("config.yaml", ".env", "state.db")

def _is_hermes_profiles_root(profiles_dir: Path) -> bool:
    """True when *profiles_dir* is provably ``<hermes-home>/profiles``.

    Accepts the classic ``~/.hermes`` layout, a root carrying Hermes-home marker files, a
    ``profiles/.deleted`` tombstone dir (only ``profile delete`` creates it), or the default root.
    """
    root = profiles_dir.parent
    if root.name == ".hermes":
        return True
    try:
        if (profiles_dir / _DELETED_PROFILES_DIR).is_dir() or any(
            (root / marker).exists() for marker in _HERMES_HOME_MARKERS
        ):
            return True
    except OSError:
        pass
    try:
        return root.resolve(strict=False) == get_default_hermes_root().resolve(strict=False)
    except OSError:
        return False

def named_profile_home(path: str | Path) -> Path | None:
    """Return ``<root>/profiles/<name>`` when *path* is that home or under it.

    Requires ``<name>`` not to start with ``.`` and the ``profiles`` parent to be a real Hermes home;
    a default home whose path merely contains a ``profiles`` segment is not a named profile.
    """
    current = Path(path)
    for candidate in (current, *current.parents):
        if (candidate.parent.name == "profiles" and not candidate.name.startswith(".")
                and _is_hermes_profiles_root(candidate.parent)):
            return candidate
        if candidate.name == ".hermes":  # default home: a coincidental profiles/ ancestor is not a root
            return None
    return None

def profile_name_for_home(path: str | Path | None) -> str | None:
    """Return the canonical profile id owning *path*, or ``None`` when it is not a profile home.

    The default home is the Hermes root itself, so its basename is an installation detail (``.hermes``
    on POSIX and commonly ``hermes`` on Windows), not the profile id ``default``.
    """
    if path is None or not str(path).strip():
        return None
    current = Path(path).expanduser()
    try:
        default_root = get_default_hermes_root()
        for candidate in (current, current.resolve(strict=False)):
            if candidate == default_root or candidate == default_root.resolve(strict=False):
                return "default"
            named = named_profile_home(candidate)
            if named is not None:
                return named.name
            # A stored profile home is authoritative: its owner already resolved it, so the
            # <root>/profiles/<name> shape names the profile even when <root> carries no markers.
            if candidate.parent.name == "profiles" and not candidate.name.startswith("."):
                return candidate.name
    except (OSError, RuntimeError, ValueError):
        return None
    return None

def profile_tombstone_path(profile_home: Path) -> Path:
    return profile_home.parent / _DELETED_PROFILES_DIR / profile_home.name

def named_profile_is_deleted(profile_home: str | Path) -> bool:
    return profile_tombstone_path(Path(profile_home)).exists()

def assert_named_profile_home_live(path: str | Path) -> None:
    """Refuse missing or tombstoned named profile homes."""
    home = named_profile_home(path)
    if home is not None and (named_profile_is_deleted(home) or not home.exists()):
        raise FileNotFoundError(
            f"Named profile home does not exist: {home}. "
            "Create the profile explicitly before using it."
        )

def mkdir_under_hermes_home(path: str | Path) -> Path:
    """Create *path*, but never materialize a deleted/missing named profile."""
    target = Path(path)
    assert_named_profile_home_live(target)
    target.mkdir(parents=True, exist_ok=True)
    return target



def display_hermes_home(home: Path | None = None) -> str:
    """User-facing ``~/`` display string for HERMES_HOME (``~/.hermes/profiles/coder``).

    ``home`` overrides the lookup for callers that run before the CLI has applied the sticky
    ``active_profile`` (``get_hermes_home()`` would emit the wrong-profile fallback warning there).
    """
    if home is None:
        home = get_hermes_home()
    try:  # as_posix(): str() on Windows yields chimeras like ~/AppData\Local\hermes/skills/
        return "~/" + home.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(home)

def profile_cli_selector() -> str:
    """``-p <name> `` (trailing space) pinning copy-pasteable ``hermes ...`` guidance to the
    active NAMED profile, else ``""``: a bare ``hermes`` follows the sticky ``active_profile``
    file, which can name a different database than the one that failed (#105887). A custom
    home outside the profile tree has no selector (only HERMES_HOME names it)."""
    name = profile_name_for_home(get_hermes_home())
    return f"-p {name} " if name and name != "default" else ""

SCRATCH_MAX_IDLE_HOURS = 24

_SCRATCH_PRUNE_STAMP = ".last_prune"

_SCRATCH_PRUNE_INTERVAL_SECONDS = 3600

_scratch_pruned_once = False

_MANAGED_TRUE_VALUES = ("true", "1", "yes")

_LEGACY_MANAGED_SYSTEM = "nixos"

_IGNORED_MANAGED_VALUES = frozenset({"brew", "homebrew"})

_MANAGED_FALSE_VALUES = frozenset({"false", "0", "no", "off"})

def get_managed_system() -> str | None:
    """Return the package manager owning this install, if any.
    Signals: HERMES_MANAGED env var (systemd service) or a ``.managed`` marker file in
    HERMES_HOME (NixOS activation script — interactive shells don't see the service env).
    An unreadable or empty marker still counts as managed (the legacy NixOS shape)."""
    marker = os.getenv("HERMES_MANAGED", "").strip().lower() or None
    managed_marker = get_hermes_home() / ".managed"
    if marker is None and managed_marker.exists():
        try:
            marker = managed_marker.read_text(encoding="utf-8", errors="replace").strip().lower()
        except OSError:
            marker = ""
    if marker is None or marker in _IGNORED_MANAGED_VALUES or marker in _MANAGED_FALSE_VALUES:
        return None
    if marker == "" or marker in _MANAGED_TRUE_VALUES:
        return _LEGACY_MANAGED_SYSTEM
    return marker

def _container_or_chmod_skipped() -> bool:
    """Container/chmod-skip detection: the ``HERMES_CONTAINER``/``HERMES_SKIP_CHMOD`` operator
    overrides on top of the canonical :func:`_detect_container` signals (same breadth as
    :func:`is_container` — Docker/Podman/LXC/Kubernetes, cgroup and root-mountinfo). The cached
    :func:`is_container` itself is deliberately avoided: it ignores these env overrides and is
    computed only once per process, so tests could not flip it. Volume-mounted config is not
    forced to owner-only in containers: gateway and dashboard may run as different UIDs, or
    the mount itself needs broader permissions."""
    if os.environ.get("HERMES_CONTAINER") or os.environ.get("HERMES_SKIP_CHMOD"):
        return True
    return _detect_container()

def _resolve_hermes_uid_gid() -> tuple[int | None, int | None]:
    """Read HERMES_UID / HERMES_GID (set by Docker deployments); (None, None) if unset/invalid/Windows.
    The entrypoint chowns HERMES_HOME once, but subdirs created at runtime (``profiles/<name>/``)
    need the same chown or they land root:root and block later uid-mapped workers.

    Docker containers running Hermes commonly set these to map the in-container user to a host user so
    volume-mounted state files end up with the right ownership. See #34107.
    """
    if sys.platform == "win32":
        return None, None

    def _env_int(name: str) -> int | None:
        try:
            return int(os.environ.get(name, "").strip() or None)
        except (TypeError, ValueError):
            return None

    return _env_int("HERMES_UID"), _env_int("HERMES_GID")

def _chown_to_hermes_uid(path) -> None:
    """Chown ``path`` to ``HERMES_UID:HERMES_GID`` when set; EPERM/ENOENT are non-fatal (the
    entrypoint's startup chown -R fixes ownership on the next restart).

    Used by :func:`apply_secure_dir_policy` to keep ownership consistent across all directories
    created by ``ensure_hermes_home`` on Docker deployments. See #34107.
    """
    uid, gid = _resolve_hermes_uid_gid()
    if uid is None and gid is None:
        return
    try:
        os.chown(path, uid if uid is not None else -1, gid if gid is not None else -1)
    except (OSError, AttributeError, NotImplementedError):
        pass

def apply_secure_dir_policy(path) -> None:
    """Apply the canonical Hermes home-directory permission policy to *path*.

    Owner-only ``0700`` by default, but the operator's explicit and managed sharing choices
    win (#117347): managed installs are left exactly as the package manager / activation
    script set them (#77579); in a container only an explicit ``HERMES_HOME_MODE`` is applied
    (a bind-mounted data dir is often shared with sibling containers, #10757); elsewhere
    ``HERMES_HOME_MODE`` (e.g. ``0701``, ``2770``) overrides the mode. ``HERMES_UID`` /
    ``HERMES_GID`` ownership is applied when those env vars are set (#34107).

    Import-safe twin of ``hermes_cli.config._secure_dir`` (which delegates here), so callers
    outside the CLI package — like :func:`get_scratch_dir` — share one policy implementation.
    """
    if get_managed_system() is not None:
        return
    explicit_mode = os.environ.get("HERMES_HOME_MODE", "").strip()
    if _container_or_chmod_skipped() and not explicit_mode:
        _chown_to_hermes_uid(path)
        return
    try:
        mode = int(explicit_mode or "700", 8)
    except ValueError:
        mode = 0o700
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass
    _chown_to_hermes_uid(path)

def get_scratch_dir(home: str | Path | None = None, *, prune: bool = True) -> Path:
    """``<home>/cache/scratch`` (created, owner-only); *home* defaults to the active Hermes home.

    Every Hermes process and child gets ``TMPDIR``/``TMP``/``TEMP`` pointed here at boot (see
    :func:`export_scratch_tmp_env`), so ``tempfile`` defaults land here without call sites
    knowing. Entries idle for ``SCRATCH_MAX_IDLE_HOURS`` are pruned at most once per process
    and once per hour across processes (stamp file), so a fan-out of children stays cheap.

    Permissions follow :func:`apply_secure_dir_policy`, so an explicit ``HERMES_HOME_MODE`` or
    a managed/shared home is honored instead of a blanket ``0700`` (#117347).
    """
    base = Path(home) if home is not None else get_hermes_home()
    scratch = base / "cache" / "scratch"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            apply_secure_dir_policy(scratch)
    except OSError:
        pass
    if prune:
        _prune_scratch_dir_once(scratch)
    return scratch

def prune_scratch_dir(scratch: Path | None = None, max_idle_hours: float = SCRATCH_MAX_IDLE_HOURS) -> int:
    """Delete top-level scratch entries with no write anywhere in their subtree for
    *max_idle_hours*, reaping processes and git worktree registrations rooted in them
    first (``hermes_constants_scratch``); return the count removed."""
    from hermes_constants_scratch import prune_idle_entries

    root = scratch if scratch is not None else get_scratch_dir(prune=False)
    return prune_idle_entries(root, max_idle_hours, frozenset({_SCRATCH_PRUNE_STAMP}))

def _prune_scratch_dir_once(scratch: Path) -> None:
    global _scratch_pruned_once
    if _scratch_pruned_once:
        return
    _scratch_pruned_once = True
    import time
    stamp = scratch / _SCRATCH_PRUNE_STAMP
    try:
        if time.time() - stamp.stat().st_mtime < _SCRATCH_PRUNE_INTERVAL_SECONDS:
            return
    except OSError:
        pass
    with contextlib.suppress(Exception):
        stamp.touch()
        prune_scratch_dir(scratch)

def _canonical_model_variants(model: str) -> list[str]:
    """Spelling variants for tolerant override matching, exact first, deduped in order.

    Dot/dash recovery runs on EACH base form so ``x-4.5``, ``x-4-5`` and ``x.4.5`` share one variant set.
    """
    _dash_to_dot = lambda s: re.sub(r'(\d)-(\d)', r'\1.\2', s)
    _dot_to_dash = lambda s: re.sub(r'(\d)\.(\d)', r'\1-\2', s)
    seen: set[str] = set()
    variants: list[str] = []

    def _add(*values):
        for v in values:
            if v and v not in seen:
                seen.add(v)
                variants.append(v)

    def _add_with_derivatives(s):
        dashed, dotted = s.replace('.', '-'), s.replace('-', '.')
        _add(s, dashed, dotted, _dash_to_dot(s), _dot_to_dash(s), _dash_to_dot(dashed), _dot_to_dash(dotted))
    _add_with_derivatives(model)
    parts = model.split('/')
    if len(parts) >= 2:  # bare model (strip provider/aggregator prefix)
        _add_with_derivatives(parts[-1])
    if len(parts) >= 3:  # strip aggregator only: "openrouter/anthropic/x" → "anthropic/x"
        _add_with_derivatives('/'.join(parts[1:]))
    known_providers = (
        'anthropic', 'openai', 'google', 'openrouter', 'groq', 'mistral',
        'xai', 'cohere', 'perplexity', 'together', 'fireworks', 'deepseek',
    )
    for v in [v for v in variants if '/' not in v]:
        _add(*(f"{provider}/{v}" for provider in known_providers))
    known_aggregators = ('openrouter', 'opencode', 'fireworks', 'groq', 'together')
    for v in [v for v in variants if v.count('/') == 1]:
        _add(*(f"{agg}/{v}" for agg in known_aggregators))
    return variants

def resolve_per_model_provider_routing(model: str, models: dict | None) -> dict:
    """``provider_routing.models.<id>`` entry for *model*, spelling-tolerant like
    ``reasoning_overrides``; ``{}`` when none matches. Only the keys a user sets per model
    are returned so unset ones fall through to the flat ``provider_routing`` values."""
    if not model or not isinstance(models, dict):
        return {}
    for variant in _canonical_model_variants(model):
        entry = models.get(variant)
        if isinstance(entry, dict):
            return entry
    return {}

_wsl_detected: bool | None = None

def is_wsl() -> bool:
    """True inside WSL1/WSL2 (``microsoft`` marker in ``/proc/version``); cached per process."""
    global _wsl_detected
    if _wsl_detected is None:
        try:
            with open("/proc/version", "r", encoding="utf-8") as f:
                _wsl_detected = "microsoft" in f.read().lower()
        except Exception:
            _wsl_detected = False
    return _wsl_detected

def _read_proc(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""

def _proc_file_has_marker(path: str, markers: tuple[str, ...]) -> bool:
    content = _read_proc(path)
    return any(marker in content for marker in markers)

def _detect_container() -> bool:
    if (
        os.path.exists("/.dockerenv")
        or os.path.exists("/run/.containerenv")
        or os.environ.get("KUBERNETES_SERVICE_HOST")
        or _proc_file_has_marker("/proc/1/cgroup", ("docker", "podman", "/lxc/", "kubepods", "containerd", "crio"))
    ):
        return True
    # cgroup v2: /proc/1/cgroup is just "0::/"; the runtime still shows in mountinfo — but ONLY on
    # the root ("/") mount line. A host that merely *runs* containers exposes every container's
    # overlay lowerdir (``lowerdir=/var/lib/containerd/...``) at non-root mount points, which a
    # whole-file scan misread as "inside a container" and flipped subprocess HOME (#58135).
    return _root_mount_has_marker("/proc/self/mountinfo", ("kubepods", "containerd", "crio"))

def _root_mount_has_marker(path: str, markers: tuple[str, ...]) -> bool:
    """mountinfo field 5 (index 4) is the mount point; only the root ("/") line is the process's own rootfs."""
    root_lines = [line for line in _read_proc(path).splitlines() if len(f := line.split()) >= 5 and f[4] == "/"]
    return any(marker in line for line in root_lines for marker in markers)

def get_config_path() -> Path:
    """Return the path to ``config.yaml`` under HERMES_HOME."""
    return get_hermes_home() / "config.yaml"

def get_skills_dir() -> Path:
    """Return the path to the skills directory under HERMES_HOME."""
    return get_hermes_home() / "skills"

PARTIAL_STREAM_STUB_ID = "partial-stream-stub"

FINISH_REASON_LENGTH = "length"
