# Ported from hermes-agent hermes_cli/config.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Configuration loading for Mertina Agent: config.yaml and .env."""

import copy
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from mertina.constants import (
    get_mertina_home,
)
from mertina.utils import fast_safe_load, file_signature

logger = logging.getLogger(__name__)


# (config_path, mtime_ns, size) tuples already warned about, so concurrent
# loads of a broken config.yaml don't spam stderr. A changed file (new mtime) warns again.
_CONFIG_PARSE_WARNED: set[tuple[str, int, int, int, int]] = set()


_PARSE_FAILURE_FALLBACK_MSG = {
    "last-known-good": "Mertina is running on the settings it loaded before the edit until it is fixed, so recent changes are not applied.",  # noqa: E501  # upstream's message
}
_PARSE_FAILURE_DEFAULTS_MSG = "Mertina is running on default settings until it is fixed, so none of your saved settings are applied."  # noqa: E501  # upstream's message
_PARSE_FAILURE_REPAIR_MSG = "Open it in an editor and fix {where}."


def _yaml_error_location(exc: Exception) -> str:
    """``"line 12"`` from a PyYAML problem mark (1-based), else ``""``."""
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    line = getattr(mark, "line", None)
    return f"line {line + 1}" if isinstance(line, int) else ""


def _yaml_error_details(exc: Exception) -> str:
    """Single-line ``Details:`` text: the PyYAML problem, or the exception's first line."""
    problem = getattr(exc, "problem", None)
    text = f"{problem}" if problem else str(exc).strip()
    return " ".join(text.split())


def format_config_parse_failure(
    config_path: Path, exc: Exception, *, fallback: str = "defaults"
) -> str:
    """User copy for an unparseable config.yaml: what happened, what Mertina is doing, how to fix.
    Only the problem line/column is printed; the raw PyYAML text goes to a ``Details:`` line."""
    where = _yaml_error_location(exc)
    at = f" at {where}" if where else ""
    fallback_msg = _PARSE_FAILURE_FALLBACK_MSG.get(fallback, _PARSE_FAILURE_DEFAULTS_MSG)
    repair = _PARSE_FAILURE_REPAIR_MSG.format(where=where or "the problem")
    return f"Your settings file ({config_path}) has a formatting error{at}. {fallback_msg} {repair}"


def _warn_config_parse_failure(
    config_path: Path, exc: Exception, *, fallback: str = "defaults"
) -> None:
    """Surface a config.yaml parse failure to log and stderr (once per file signature).
    Silent fallback to ``DEFAULT_CONFIG`` drops every user override, so this must be loud.

    ``fallback`` selects the message wording: ``"defaults"`` (fresh process, nothing else to
    serve) or ``"last-known-good"`` (in-process retention of the previously loaded config).
    """
    try:
        st = config_path.stat()
        sig = file_signature(st)
        key = (str(config_path), *sig)
    except OSError:
        key = (str(config_path), 0, 0, 0, 0)
    if key in _CONFIG_PARSE_WARNED:
        return
    _CONFIG_PARSE_WARNED.add(key)
    msg = format_config_parse_failure(config_path, exc, fallback=fallback)
    logger.warning("%s Details: %s", msg, _yaml_error_details(exc))
    try:
        sys.stderr.write(f"⚠️  mertina config: {msg}\n    Details: {_yaml_error_details(exc)}\n")
        sys.stderr.flush()
    except Exception:
        pass


# Serializes config loads and guards the module-level caches below. libyaml's C extension is
# not thread-safe for concurrent safe_load() on one file, and tool threads load config
# concurrently during long agent runs.
_CONFIG_LOCK = threading.RLock()
# path -> last successfully loaded (expanded) config; served after a parse failure so a
# mid-edit broken YAML never silently drops user overrides.
_LAST_EXPANDED_CONFIG_BY_PATH: dict[str, Any] = {}
# path -> (file signature, four zeros where upstream folds in the managed file, merged,
# env_ref_snapshot). load_config() returns a deepcopy of the cached value while the signature
# matches (skips safe_load + merge + normalize + expand, ~13 ms). The env snapshot invalidates
# when a referenced ${VAR} changes value (in-process rotation). See #58514.
_LOAD_CONFIG_CACHE: dict[str, tuple[Any, ...]] = {}


def get_config_path() -> Path:
    """Get the main config file path."""
    return get_mertina_home() / "config.yaml"


def get_env_path() -> Path:
    """Get the .env file path (for API keys)."""
    return get_mertina_home() / ".env"


from mertina.cli.config_defaults import DEFAULT_CONFIG  # noqa: E402


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*: dict-over-dict recurses (so overriding one leaf
    keeps sibling defaults), and ``None`` over a dict section is ignored.

    An empty section key in config.yaml (``web:`` with no value) parses as YAML ``None``;
    treating that as an override would replace the entire default dict with ``None`` and crash
    every downstream consumer that expects a mapping (#58277).
    """
    result = base.copy()
    for key, value in override.items():
        over_dict = isinstance(result.get(key), dict)
        if over_dict and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif not (over_dict and value is None):
            result[key] = value
    return result


_ENV_REF_RE = re.compile(r"\${([^}]+)}")


def _env_ref_lookup(name: str) -> str | None:
    """Resolve the env var behind a ``${VAR}`` / ``${env:VAR}`` ref from ``os.environ``."""
    return os.environ.get(name)


def _env_expand_match(m: re.Match[str]) -> str:
    """Expand one ``${VAR}`` (legacy bare name) or ``${env:VAR}`` (Cursor-style SecretRef).
    Other SecretRef sources (``file:``, ``bitwarden:``, ``vault:``...) are NOT resolved here:
    external backends inject their values into the environment at startup (the ``secrets:``
    block), so a config ref only ever needs the env shape. Unresolved refs stay verbatim so
    callers can detect them."""
    raw: str = m.group(0)
    inner = m.group(1).strip()
    name = _env_ref_var_name(inner)
    if name is None:
        if not inner.startswith("env:") and _is_non_env_secret_ref(inner):
            logger.warning(
                "Config ref %r uses source %r which is not resolvable in "
                "config.yaml — external secret sources inject env vars at "
                "startup, so reference the variable as ${env:NAME} instead",
                raw,
                inner.split(":", 1)[0],
            )
        return raw  # non-env source, or empty ``${env:}``
    val = _env_ref_lookup(name)
    if val is not None:
        return val
    if inner.startswith("env:"):
        logger.warning(
            "Config ref %r: %s is not set (check ~/.mertina/.env); keeping the literal placeholder",
            raw,
            name,
        )
    return raw


def _is_non_env_secret_ref(ref: str) -> bool:
    """True for a SecretRef body with a non-``env`` source (``bitwarden:FOO``, ``vault:...``)."""
    return ":" in ref and re.match(r"^[a-z][a-z0-9_-]*:", ref) is not None


def _env_ref_var_name(ref: str) -> str | None:
    """Env-var name a ``${...}`` body reads, or None for a non-env source / empty ``env:``."""
    ref = ref.strip()
    if ref.startswith("env:"):
        return ref[len("env:") :].strip() or None
    if _is_non_env_secret_ref(ref):
        return None
    return ref


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${env:VAR}`` in string values (keys/non-strings
    untouched)."""
    if isinstance(obj, str):
        return _ENV_REF_RE.sub(_env_expand_match, obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj


def _env_ref_snapshot(
    obj: Any, snapshot: dict[str, str | None] | None = None
) -> dict[str, str | None]:
    """Map each env-sourced ``${...}`` ref in *obj* to its current value.
    Stored with cached ``load_config()`` results so a cache hit can detect that the expansion was
    made against a different environment (in-process rotation) — file mtime/size alone cannot
    see it.

    See #58514.
    """
    if snapshot is None:
        snapshot = {}
    if isinstance(obj, str):
        for raw in _ENV_REF_RE.findall(obj):
            name = _env_ref_var_name(raw)
            if name is not None:
                snapshot[name] = _env_ref_lookup(name)
    elif isinstance(obj, dict):
        for value in obj.values():
            _env_ref_snapshot(value, snapshot)
    elif isinstance(obj, list):
        for item in obj:
            _env_ref_snapshot(item, snapshot)
    return snapshot


def _normalize_root_model_keys(config: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize the ``model`` section at the load chokepoint.
    Root-level ``provider``/``base_url``/``context_length`` (older layouts) are moved under
    ``model`` only when the corresponding ``model.*`` key is empty — never overriding. ``api_base``
    (the OpenAI-SDK/LiteLLM name users reach for) is an alias for ``base_url``; readers use
    only ``model.base_url`` (issue #8919). A dict-valued ``default``/``model``/``name`` is
    flattened so no reader sees a nested dict, and the id is canonicalized to ``default``
    (issue #34500). Precedence: ``default`` > ``model`` > ``name`` (never overrides an explicit
    ``default``).
    """
    model_in = config.get("model")
    needs_model_work = isinstance(model_in, dict) and (
        model_in.get("api_base")
        or model_in.get("model")
        or model_in.get("name")
        or any(isinstance(model_in.get(k), dict) for k in ("default", "model", "name"))
    )
    has_root = any(config.get(k) for k in ("provider", "base_url", "context_length", "api_base"))
    if not has_root and not needs_model_work:
        return config

    config = dict(config)
    model = config.get("model")
    model = dict(model) if isinstance(model, dict) else {"default": model} if model else {}
    config["model"] = model

    # Flatten ``{provider: <p>, model: <m>}``. The nested provider wins over the merged default
    # ``"auto"`` (which runtime resolution treats as authoritative) but never over a configured one.
    for _key in ("default", "model", "name"):
        _val = model.get(_key)
        if isinstance(_val, dict):
            _nested_model = _val.get("model") or _val.get("default")
            _nested_provider = str(_val.get("provider") or "").strip()
            model[_key] = str(_nested_model or "").strip()
            if _nested_provider:
                _outer_provider = str(model.get("provider") or "").strip()
                if not _outer_provider or _outer_provider == "auto":
                    model["provider"] = _nested_provider

    for key in ("provider", "base_url", "context_length"):
        root_val = config.get(key)
        if root_val and not model.get(key):
            model[key] = root_val
        config.pop(key, None)

    for alias_val in (config.get("api_base"), model.get("api_base")):
        if alias_val and not model.get("base_url"):
            model["base_url"] = alias_val
    config.pop("api_base", None)
    model.pop("api_base", None)

    # ``model``/``name`` are last-resort aliases (in that order), then dropped.
    alias = model.get("model") or model.get("name")
    if not model.get("default") and alias:
        model["default"] = alias
    if model.get("default"):
        model.pop("model", None)
        model.pop("name", None)

    return config


def _normalize_max_turns_config(config: dict[str, Any]) -> dict[str, Any]:
    """Move legacy root-level ``max_turns`` under ``agent``; the schema default is injected only
    when the user set max_turns somewhere."""
    config = dict(config)
    agent_config = dict(config.get("agent") or {})
    if "max_turns" in config and "max_turns" not in agent_config:
        agent_config["max_turns"] = config["max_turns"]
    config["agent"] = agent_config
    config.pop("max_turns", None)
    return config


def _canonicalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """The load normalization pipeline: max_turns relocation, then model-section canon."""
    return _normalize_root_model_keys(_normalize_max_turns_config(config))


def load_config() -> dict[str, Any]:
    """Load the merged configuration (DEFAULT_CONFIG + config.yaml, env-expanded).
    Cached on the file signature; returns a deepcopy since most call sites mutate the result.
    Read-only hot paths should use ``load_config_readonly()`` to skip the deepcopy."""
    return _load_config_impl(want_deepcopy=True)


def load_config_readonly() -> dict[str, Any]:
    """``load_config()`` without the defensive deepcopy (~half of the 265us cache-hit cost).
    **Mutating the returned dict (or any nested structure) corrupts the in-process cache for
    every subsequent caller** — only for code paths that never write to the result."""
    return _load_config_impl(want_deepcopy=False)


def _load_config_cache_sig(
    config_path: Path,
) -> tuple[tuple[int, int, int, int] | None, tuple[int, ...] | None]:
    """Return ``(user_sig, cache_sig)`` for ``_LOAD_CONFIG_CACHE``.
    ``cache_sig`` is None only when config.yaml does not exist (nothing to cache on)."""
    try:
        st = config_path.stat()
        user_sig: tuple[int, int, int, int] | None = file_signature(st)
    except FileNotFoundError:
        user_sig = None
    managed_sig = (0, 0, 0, 0)
    if user_sig is None:
        return None, None
    return user_sig, (*(user_sig or (0, 0, 0, 0)), *managed_sig)  # type: ignore[unreachable]  # set here once the managed scope is cut


def _last_known_good_fallback(
    config_path: Path, path_key: str, cache_sig: tuple[int, ...] | None, exc: Exception
) -> dict[str, Any] | None:
    """Warn about a parse failure and return the last-known-good config, or None (-> defaults).
    A parse failure must not silently replace the effective config with defaults — that drops
    EVERY user override when a user mid-edits config.yaml into broken YAML. Within a running
    process, keep serving the last successfully loaded config until the file is fixed (#31188)."""
    lkg = _LAST_EXPANDED_CONFIG_BY_PATH.get(path_key)
    fallback = "last-known-good"
    _warn_config_parse_failure(
        config_path, exc, fallback=fallback if lkg is not None else "defaults"
    )
    if lkg is None:
        return None
    # Expand defensively — idempotent when already expanded.
    lkg_copy: dict[str, Any] = _expand_env_vars(copy.deepcopy(lkg))
    if cache_sig is not None:
        # Cache under the corrupt file's signature (empty env snapshot: always valid) so repeated
        # loads don't re-parse; fixing the file changes the signature and reloads normally.
        _LOAD_CONFIG_CACHE[path_key] = (*cache_sig, lkg_copy, {})
    return lkg_copy


def _load_config_impl(*, want_deepcopy: bool) -> dict[str, Any]:
    with _CONFIG_LOCK:
        config_path = get_config_path()
        path_key = str(config_path)

        user_sig, cache_sig = _load_config_cache_sig(config_path)

        cached = _LOAD_CONFIG_CACHE.get(path_key)
        if cached is not None and cache_sig is not None and cached[:8] == cache_sig:
            # Signatures match, but the cached expansion is only valid if every ${VAR} it was
            # expanded against still has the same value (#58514).
            env_snapshot = cached[9] if len(cached) > 9 else {}
            if all(_env_ref_lookup(k) == v for k, v in env_snapshot.items()):
                return copy.deepcopy(cached[8]) if want_deepcopy else cached[8]

        config = copy.deepcopy(DEFAULT_CONFIG)

        if user_sig is not None:
            try:
                with open(config_path, encoding="utf-8") as f:  # noqa: PTH123  # upstream's open()
                    user_config = fast_safe_load(f) or {}

                if "max_turns" in user_config:
                    agent_user_config = dict(user_config.get("agent") or {})
                    if agent_user_config.get("max_turns") is None:
                        agent_user_config["max_turns"] = user_config["max_turns"]
                    user_config["agent"] = agent_user_config
                    user_config.pop("max_turns", None)

                config = _deep_merge(config, user_config)
            except Exception as e:
                lkg_copy = _last_known_good_fallback(config_path, path_key, cache_sig, e)
                if lkg_copy is not None:
                    return copy.deepcopy(lkg_copy) if want_deepcopy else lkg_copy

        normalized = _canonicalize_config(config)
        expanded: dict[str, Any] = _expand_env_vars(normalized)
        _LAST_EXPANDED_CONFIG_BY_PATH[path_key] = copy.deepcopy(expanded)
        if cache_sig is not None:
            # The cache stores its own deepcopy so load_config() callers can mutate freely while
            # load_config_readonly() callers all see the same stable object. The env snapshot
            # records the values this expansion was made against so later loads detect drift.
            cached_copy = copy.deepcopy(expanded)
            env_snapshot = _env_ref_snapshot(normalized)
            _LOAD_CONFIG_CACHE[path_key] = (*cache_sig, cached_copy, env_snapshot)
            # Readonly path returns the same object later calls will see (identity invariant).
            if not want_deepcopy:
                return cached_copy
        else:
            _LOAD_CONFIG_CACHE.pop(path_key, None)
        # First-load result is a fresh dict (not aliased to the cache); safe to return directly.
        return expanded


def load_env() -> dict[str, str]:
    """Load ~/.mertina/.env as a dict. Memoised inside ``load_env_file``. Each assignment's
    value is opaque data for boundary discovery."""
    from mertina.agent.secret_scope import load_env_file  # the one .env tokenizer

    return load_env_file(get_env_path())


def _scoped_environ_get(key: str) -> str | None:
    """Read ``key`` from ``os.environ``."""
    return os.environ.get(key)


def get_env_value(key: str) -> str | None:
    """Get a value from ``os.environ``, else ``~/.mertina/.env``."""
    val = _scoped_environ_get(key)
    return load_env().get(key) if val is None else val
