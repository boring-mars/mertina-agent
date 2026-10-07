# Ported from hermes-agent tools/environments/local.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Child-process environment construction: the credential scrub for spawned subprocesses."""

from collections.abc import Callable, Mapping

from mertina.tools.environments.local_env_policy import (
    _MERTINA_PROVIDER_ENV_FORCE_PREFIX,
    _is_mertina_internal_secret,
    _is_provider_env_blocklisted,
    _plugin_terminal_env_strip_keys,
)


# --- Child-process environment construction ---
def _filter_secret_env(
    items: Mapping[str, str],
    out: dict[str, str],
    *,
    unwrap_force: bool,
    plugin_strip: frozenset[str] = frozenset(),
) -> None:
    """Copy *items* into *out*, dropping Mertina-managed secrets. ``_MERTINA_FORCE_<NAME>``
    unwraps to ``NAME`` when ``unwrap_force`` (caller extras), else is dropped."""
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
        if _is_provider_env_blocklisted(key):
            continue
        if value is not None:
            out[key] = value


def _scrubbed_env(
    parts: list[tuple[Mapping[str, str], bool]],
    plugin_strip: frozenset[str],
    fix_path: Callable[[str], str],
) -> dict[str, str]:
    """Filter each ``(items, unwrap_force)`` in *parts* into one env."""
    out: dict[str, str] = {}
    for items, unwrap_force in parts:
        _filter_secret_env(items, out, unwrap_force=unwrap_force, plugin_strip=plugin_strip)
    return out


def _sanitize_subprocess_env(
    base_env: dict[str, str] | None, extra_env: dict[str, str] | None = None
) -> dict[str, str]:
    """Filter Mertina-managed secrets from a subprocess environment (search workers)."""
    return _scrubbed_env(
        [(base_env or {}, False), (extra_env or {}, True)],
        _plugin_terminal_env_strip_keys(),
        lambda p: p,
    )
