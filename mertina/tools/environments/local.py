# Ported from hermes-agent tools/environments/local.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Local execution environment — spawn-per-call with session snapshot."""

import os
import platform
import shutil
import sys
from collections.abc import Mapping

from mertina.tools.environments.local_env_policy import (  # noqa: F401 — _MERTINA_PROVIDER_ENV_BLOCKLIST stays importable from here
    _ALWAYS_STRIP_KEYS,
    _MERTINA_PROVIDER_ENV_BLOCKLIST,
    _MERTINA_PROVIDER_ENV_FORCE_PREFIX,
    _is_mertina_internal_secret,
    _is_provider_env_blocklisted,
    _is_terminal_first_party_env,
    _matches_terminal_first_party_prefix,
    _plugin_terminal_env_strip_keys,
    strip_profile_gate_env,
)
from mertina.tools.environments.local_pythonpath import (
    _strip_mertina_owned_pythonpath_and_runtime_markers,
)

_IS_WINDOWS = platform.system() == "Windows"


# --- Child-process environment construction ---
def _apply_profile_home(env: dict) -> None:
    """Bridge the context-local MERTINA_HOME override, then the subprocess HOME contract."""
    from mertina.constants import apply_subprocess_home_env, get_mertina_home_override

    try:
        if value := get_mertina_home_override():
            env["MERTINA_HOME"] = value
    except Exception:
        pass
    apply_subprocess_home_env(env)


def _inject_session_context_env(env: dict) -> None:
    """Bridge gateway session ContextVars (MERTINA_SESSION_*) into a child env.
    Cross-session leak guard: the vars' last-writer-wins ``os.environ`` mirror may
    belong to another turn on a concurrent multi-session host, so once the session
    context is engaged ContextVars are authoritative — a bound value (incl. "") wins
    and an _UNSET var is STRIPPED, not inherited. An unengaged CLI keeps the mirror."""
    try:
        from mertina.gateway.session_context import _UNSET, _VAR_MAP, session_context_engaged
    except Exception:
        return
    _engaged = session_context_engaged()
    for var_name, var in _VAR_MAP.items():
        value = var.get()
        if value is not _UNSET:
            env[var_name] = "" if value is None else str(value)
        elif _engaged:
            env.pop(var_name, None)


def _filter_secret_env(
    items: Mapping[str, str],
    out: dict,
    *,
    unwrap_force: bool,
    plugin_strip: frozenset = frozenset(),
) -> None:
    """Copy *items* into *out*, dropping Mertina-managed secrets. ``_MERTINA_FORCE_<NAME>``
    unwraps to ``NAME`` when ``unwrap_force`` (caller extras / terminal env), else is
    dropped. Blocklisted names survive only via env_passthrough registration or as
    context-entitled first-party ``BUZZ_*`` vars; the latter are used directly, never
    scope-resolved (UnscopedSecretError under multiplex)."""
    try:
        from mertina.tools.env_passthrough import is_env_passthrough, resolve_passthrough_value
    except Exception:
        is_env_passthrough, resolve_passthrough_value = (lambda _: False), (lambda _n, fb: fb)
    plugin_strip_folded = frozenset(k.upper() for k in plugin_strip)
    for key, value in items.items():
        if key.startswith(_MERTINA_PROVIDER_ENV_FORCE_PREFIX):
            if not unwrap_force:
                continue
            key = key[len(_MERTINA_PROVIDER_ENV_FORCE_PREFIX) :]
            if not _is_mertina_internal_secret(key):
                out[key] = value
            continue
        if _is_mertina_internal_secret(key) or key.upper() in plugin_strip_folded:
            continue
        first_party = _is_terminal_first_party_env(key)
        passthrough = is_env_passthrough(key)
        if _is_provider_env_blocklisted(key) and not (passthrough or first_party):
            continue
        if passthrough and not first_party:
            value = resolve_passthrough_value(key, value)
        if value is not None:
            out[key] = value


def _finalize_child_env(env: dict) -> dict:
    """Guards shared by every spawn surface: profile-home propagation, session-context
    bridging, Mertina-owned PYTHONPATH + venv-marker strip, MSYS defaults, delegate_task
    Kanban scrub. Returns the (possibly new) dict."""
    _apply_profile_home(env)
    _inject_session_context_env(env)
    _strip_mertina_owned_pythonpath_and_runtime_markers(env)
    _apply_windows_msys_bash_env_defaults(env)
    from mertina.agent.delegation_context import delegated_child_subprocess_env

    return delegated_child_subprocess_env(env)


def _scrubbed_env(parts, plugin_strip: frozenset, fix_path) -> dict:
    """Filter each ``(items, unwrap_force)`` in *parts* into one env, rewrite PATH via
    *fix_path* (always prepending the mertina install dir so bare ``mertina`` resolves
    for children of a systemd/cron-launched gateway), then apply the shared guards."""
    out: dict[str, str] = {}
    for items, unwrap_force in parts:
        _filter_secret_env(items, out, unwrap_force=unwrap_force, plugin_strip=plugin_strip)
    # Declared names the bound profile scope holds but the process env never did (a routed
    # profile's own .env / sources) — the filter above can only see names already present.
    # Unguarded on purpose: a scope/config failure here must be loud, not silently drop the
    # declared secret again (#114209); _scrub_child_env calls it the same way.
    from mertina.tools.env_passthrough import scoped_passthrough_additions

    out.update(
        (k, v) for k, v in scoped_passthrough_additions(out).items() if k not in plugin_strip
    )
    path_key = _path_env_key(out)
    # Keep bare ``mertina`` invocations available to child jobs even when the gateway was launched by a
    # service manager or cron without the console script's directory on PATH. The terminal environment
    # already applies this invariant; Cron scripts use this sanitizer directly (#92998).
    if path_key is not None:
        out[path_key] = _prepend_mertina_bin_dir(fix_path(out.get(path_key, "")))
    return _finalize_child_env(out)


def _sanitize_subprocess_env(base_env: dict | None, extra_env: dict | None = None) -> dict:
    """Filter Mertina-managed secrets from a subprocess environment (background/PTY
    spawn path, search workers, computer-use driver, user-script runners)."""
    return _scrubbed_env(
        [(base_env or {}, False), (extra_env or {}, True)],
        _plugin_terminal_env_strip_keys(),
        lambda p: p,
    )


def _prepend_missing_path_entries(existing_path: str, dirs: list[str]) -> str:
    """Prepend *dirs* missing from *existing_path* (``os.pathsep``); an already-listed
    dir keeps its position; unchanged input when nothing is missing."""
    entries = [e for e in existing_path.split(os.pathsep) if e]
    missing = [d for d in dirs if d not in entries]
    return os.pathsep.join([*missing, *entries]) if missing else existing_path


# Cached directory containing the ``mertina`` console-script.
# ``_SENTINEL`` distinguishes "not resolved yet" from a resolved ``None``.
_SENTINEL = object()


def _resolve_mertina_bin_dir() -> str | None:
    """Directory holding the ``mertina`` console-script, or None (cached). A gateway
    launched by systemd/cron/a desktop launcher lacks the install dir on PATH and bare
    ``mertina`` exits 127. Order: ``which``; absolute ``sys.argv[0]`` naming a real
    mertina executable; ``sys.executable``'s dir if it holds the shim."""
    global _MERTINA_BIN_DIR
    if _MERTINA_BIN_DIR is not _SENTINEL:
        return _MERTINA_BIN_DIR  # type: ignore[return-value]
    which = shutil.which("mertina")
    argv0 = sys.argv[0] if sys.argv else ""
    base = os.path.basename(argv0).lower()
    exe_dir = os.path.dirname(sys.executable) if sys.executable else ""
    shim = "mertina.exe" if _IS_WINDOWS else "mertina"
    if which:
        candidate = os.path.dirname(which)
    elif (
        os.path.isabs(argv0)
        and (base == "mertina" or base.startswith("mertina."))
        and os.path.isfile(argv0)
    ):
        candidate = os.path.dirname(argv0)
    else:
        candidate = exe_dir if exe_dir and os.path.isfile(os.path.join(exe_dir, shim)) else None
    _MERTINA_BIN_DIR = candidate if candidate and os.path.isdir(candidate) else None
    return _MERTINA_BIN_DIR


def _prepend_mertina_bin_dir(existing_path: str) -> str:
    """Prepend the mertina install dir to ``existing_path`` if missing."""
    bin_dir = _resolve_mertina_bin_dir()
    return _prepend_missing_path_entries(existing_path, [bin_dir] if bin_dir else [])


def _apply_windows_msys_bash_env_defaults(env: dict) -> None:
    """Disable MSYS argument path conversion (``/FO`` -> ``C:/.../git/FO`` breaks
    tasklist/schtasks/wmic/``cmd /c``). Git for Windows honors ``MSYS_NO_PATHCONV``;
    MSYS2/Cygwin bash honor ``MSYS2_ARG_CONV_EXCL`` — set both; users can override.

    Git Bash rewrites arguments that look like Unix paths (``/FO``, ``/TN``, ``/Create``) into
    ``C:/.../git/FO``-style paths, which breaks native Windows commands such as ``tasklist``, ``schtasks``,
    and ``wmic``. Mertina runs terminal commands through bash on Windows, so set the standard MSYS opt-out by
    default. Refs #56700.
    MSYS2-proper and Cygwin bash (which ``_find_bash`` can still return via the final ``shutil.which``
    fallback) ignore it and honor ``MSYS2_ARG_CONV_EXCL`` instead, so set both. ``*`` disables all argv
    conversion — the semantic equivalent of ``MSYS_NO_PATHCONV=1``. Also fixes ``cmd /c`` mangling (#56147).
    """
    if _IS_WINDOWS:
        env.setdefault("MSYS_NO_PATHCONV", "1")
        env.setdefault("MSYS2_ARG_CONV_EXCL", "*")


def _path_env_key(run_env: dict) -> str | None:
    """PATH env key to update without altering Windows casing (``Path`` vs ``PATH``);
    None when a Windows env has no PATH key at all."""
    return next((k for k in run_env if k.upper() == "PATH"), None) if _IS_WINDOWS else "PATH"
