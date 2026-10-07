# Ported from hermes-agent hermes_cli/plugins.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Mertina Plugin System — discovers and loads the bundled plugins.

Source: bundled ``mertina/plugins/<category>/<name>/``. A directory plugin needs a ``plugin.yaml``
manifest and an ``__init__.py`` exposing ``register(ctx)``; plugins register providers through
``PluginContext.register_<category>_provider()``.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import sys
import threading
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mertina.cli.plugins_discovery import (  # noqa: F401 — re-exported
    _get_disabled_plugins,
    _get_enabled_plugins,
    collect_directory_manifests,
    gate_manifest,
    scan_directory,
)
from mertina.cli.plugins_ledger import PluginLedgerMixin, PluginRegistration
from mertina.cli.plugins_loader import (
    PluginLoaderMixin,
    _serialized_replacement,
)

# Sibling modules' names are re-exported here (origin) so plugins and tests keep one import path.
from mertina.cli.plugins_manifest import (  # noqa: F401 — re-exported
    _CONFIG_SCHEMA_TYPES,
    SUPPORTED_MANIFEST_VERSION,
    PluginManifest,
    manifest_key,
    parse_manifest_file,
    resolve_plugin_load_order,
)
from mertina.constants import get_mertina_home, mertina_home_key
from mertina.utils import env_var_enabled


def get_bundled_plugins_dir() -> Path:
    """Bundled ``plugins/`` dir: ``MERTINA_BUNDLED_PLUGINS`` (Nix wrapper / packaged installs,
    read-only store paths) first, else the in-repo path."""
    env_override = os.getenv("MERTINA_BUNDLED_PLUGINS")
    if env_override:
        return Path(env_override)
    return Path(__file__).resolve().parent.parent / "plugins"


logger = logging.getLogger(__name__)


# ``MERTINA_PLUGINS_DEBUG=1`` tees verbose discovery logs to stderr. Read
# once at import; tests flip it mid-process via ``_install_plugin_debug_handler(force=True)``.
_PLUGINS_DEBUG = env_var_enabled("MERTINA_PLUGINS_DEBUG")


_DEBUG_HANDLER_INSTALLED = False


def _install_plugin_debug_handler(force: bool = False) -> None:
    """When MERTINA_PLUGINS_DEBUG is on, tee plugin logs to stderr at DEBUG (once per process)."""
    global _DEBUG_HANDLER_INSTALLED, _PLUGINS_DEBUG
    if force:
        _PLUGINS_DEBUG = env_var_enabled("MERTINA_PLUGINS_DEBUG")
    if not _PLUGINS_DEBUG or _DEBUG_HANDLER_INSTALLED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("[plugins] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    _DEBUG_HANDLER_INSTALLED = True
    logger.debug("MERTINA_PLUGINS_DEBUG=1 — verbose plugin discovery logging enabled")


_install_plugin_debug_handler()


@dataclass
class LoadedPlugin:
    """Runtime state for a single loaded plugin."""

    manifest: PluginManifest
    module: types.ModuleType | None = None
    enabled: bool = False
    error: str | None = None


class PluginContext:
    """Facade given to plugins so they can register providers."""

    def __init__(self, manifest: PluginManifest, manager: PluginManager):
        self.manifest = manifest
        self._manager = manager

    def _wrong_type(self, obj: Any, base_class: type, label: str, article: str = "a") -> bool:
        """Warn-and-ignore gate shared by every registrar that requires a base class."""
        if isinstance(obj, base_class):
            return False
        logger.warning(
            "Plugin '%s' tried to register %s %s that does not inherit from %s. Ignoring.",
            self.manifest.name,
            article,
            label,
            base_class.__name__,
        )
        return True

    def _register_scoped_provider(
        self,
        provider: Any,
        *,
        kind: str,
        base_class: type,
        registry: Any,
        label: str,
        article: str = "a",
        normalize: Callable[[str], str] | None = lambda n: n.strip(),
        register: Callable[..., Any] | None = None,
        reject_message: str | None = None,
    ) -> PluginRegistration | None:
        """Shared body of the ``register_<category>_provider`` methods: type-check (warn + ignore),
        register in the scope-keyed ``registry``, lease the slot so unload restores the displaced
        entry. ``None`` when the registry refused/replaced the provider (``ValueError`` with
        ``reject_message`` set, or a falsy ``register``)."""
        if self._wrong_type(provider, base_class, label, article):
            return None
        registry_name = provider.name if normalize is None else normalize(provider.name)
        scope = self._manager.scope_key
        previous = registry.snapshot_registration(registry_name, scope=scope)
        try:
            accepted = (register or registry.register_provider)(provider, scope=scope)
        except ValueError as exc:
            if reject_message is None:
                raise
            logger.warning(reject_message, self.manifest.name, exc)
            return None
        if (register is not None and not accepted) or registry.snapshot_registration(
            registry_name, scope=scope
        ) is not provider:
            return None
        handle = self._manager._track_scoped_registration(
            self.manifest, kind, registry_name, registry, provider, previous
        )
        logger.info("Plugin '%s' registered %s: %s", self.manifest.name, label, registry_name)
        return handle


# -- scoped provider registrars ------------------------------------------------------------------
# Every ``register_<category>_provider`` shares one body
# (:meth:`PluginContext._register_scoped_provider`): type-check, register in the scope-keyed
# process-global registry, lease the slot so unload restores the displaced entry. Rows: (method,
# kind, registry module, base-class module:attr, label, docstring, options). ``normalize``:
# ``strip`` (default), ``lower`` (strip+lowercase) or ``None`` (raw name).
_SCOPED_PROVIDER_REGISTRARS: tuple[tuple[str, str, str, str, str, str, dict[str, Any]], ...] = (
    (
        "register_web_search_provider",
        "web_search_provider",
        "mertina.agent.web_search_registry",
        "mertina.agent.web_search_provider:WebSearchProvider",
        "web provider",
        "Register an :class:`agent.web_search_provider.WebSearchProvider`; "
        "``provider.name`` is matched by ``web.search_backend`` / ``web.extract_backend`` / ``web.backend``.",  # noqa: E501  # upstream's text
        {},
    ),
)


_NAME_NORMALIZERS: dict[str | None, Callable[[str], str] | None] = {
    "strip": lambda n: n.strip(),
    "lower": lambda n: n.strip().lower(),
    None: None,
}


def _make_scoped_provider_registrar(
    method_name: str,
    kind: str,
    registry_mod: str,
    base_ref: str,
    label: str,
    doc: str,
    options: dict[str, Any],
) -> Callable[..., PluginRegistration | None]:
    """Build one ``register_<category>_provider`` method from a ``_SCOPED_PROVIDER_REGISTRARS``
    row."""
    base_mod, base_attr = base_ref.split(":")
    normalize_fn = _NAME_NORMALIZERS[options.get("normalize", "strip")]
    register_name = options.get("register")

    def register(self: PluginContext, provider: Any) -> PluginRegistration | None:
        registry = importlib.import_module(registry_mod)
        return self._register_scoped_provider(
            provider,
            kind=kind,
            base_class=getattr(importlib.import_module(base_mod), base_attr),
            registry=registry,
            label=label,
            article=options.get("article", "a"),
            normalize=normalize_fn,
            register=getattr(registry, register_name) if register_name else None,
            reject_message=options.get("reject_message"),
        )

    # secret sources: ``source``
    def register_source(self: PluginContext, source: Any) -> PluginRegistration | None:
        return register(self, source)

    method = register_source if options.get("param") == "source" else register
    method.__name__, method.__qualname__, method.__doc__ = (
        method_name,
        f"PluginContext.{method_name}",
        doc,
    )
    return _serialized_replacement(method)


for _row in _SCOPED_PROVIDER_REGISTRARS:
    setattr(PluginContext, _row[0], _make_scoped_provider_registrar(*_row))


del _row


class PluginManager(PluginLoaderMixin, PluginLedgerMixin):
    """Central manager that discovers and loads plugins."""

    def __init__(self, scope_key: str | None = None) -> None:
        self.scope_key = scope_key or mertina_home_key()
        self._discovery_lock = threading.RLock()
        self._discovered: bool = False
        self._plugins: dict[str, LoadedPlugin] = {}
        self._ownership_ledger: dict[str, list[PluginRegistration]] = {}
        self._registration_order: list[PluginRegistration] = []

    def discover_and_load(self, force: bool = False) -> None:
        """Scan the plugin sources and load each plugin found (once; ``force`` scans again)."""
        with self._discovery_lock:
            if self._discovered and not force:
                return
            if env_var_enabled("MERTINA_SAFE_MODE"):
                logger.info("MERTINA_SAFE_MODE=1 — plugin discovery skipped")
                self._discovered = True
                return
            # Flag set up front as a re-entrancy guard (register() can trigger discovery again) but
            # reset on failure so a failed scan is NOT cached as "discovered with an empty registry"
            # — callers swallow the exception and would be stranded on the early return above.
            self._discovered = True
            try:
                self._discover_and_load_inner()
            except BaseException:
                self._discovered = False
                raise

    def _discover_and_load_inner(self) -> None:
        """The actual discovery sweep — see :meth:`discover_and_load`."""
        manifests: list[PluginManifest] = self._collect_directory_manifests()
        disabled = _get_disabled_plugins()
        enabled = _get_enabled_plugins()  # None = opt-in default (nothing enabled)
        # Later sources win on key collision (project > user > bundled); gate the winners, then
        # load survivors in requires_plugins order (see resolve_plugin_load_order).
        winners = {manifest_key(m): m for m in manifests}
        to_load = {k: m for k, m in winners.items() if self._gate_manifest(m, disabled, enabled)}
        for lookup_key in resolve_plugin_load_order(to_load):
            manifest = to_load[lookup_key]
            self._load_plugin(manifest)
        if manifests:
            logger.info(
                "Plugin discovery complete: %d found, %d enabled",
                len(self._plugins),
                sum(1 for p in self._plugins.values() if p.enabled),
            )

    def _gate_manifest(
        self,
        manifest: PluginManifest,
        disabled: set[str],
        enabled: set[str] | None,
    ) -> bool:
        """Route one winning manifest per :func:`gate_manifest`: load now or record as
        skipped (introspection-only placeholder). Returns True only for plugins that go through the
        dependency-ordered load pass."""
        verdict = gate_manifest(manifest, disabled, enabled)
        if verdict.action == "load":
            return True
        if verdict.action == "load_now":
            self._load_plugin(manifest)
        else:
            self._plugins[manifest_key(manifest)] = LoadedPlugin(
                manifest=manifest, enabled=verdict.enabled, error=verdict.error
            )
        if verdict.log:
            logger.log(*verdict.log)
        return False

    def _collect_directory_manifests(self) -> list[PluginManifest]:
        """Directory manifests in full-discovery order (see :func:`collect_directory_manifests`)."""
        return collect_directory_manifests()


# Legacy single-slot "current" manager, kept so tests that monkeypatch ``_plugin_manager`` keep
# working — ``get_plugin_manager()`` still reads/writes this name.
_plugin_manager: PluginManager | None = None


# Resolved Mertina home -> PluginManager, so a changed home gets its own manager.
_plugin_managers_by_home: dict[Path, PluginManager] = {}


_plugin_managers_lock = threading.RLock()


def _plugin_home_key() -> Path:
    """Resolved active Mertina home — the key for per-profile plugin managers (plugins capture the
    home at registration, so a process serving several profiles cannot share one manager)."""
    try:
        return get_mertina_home().expanduser().resolve()
    except Exception:
        return get_mertina_home().expanduser()


def get_plugin_manager() -> PluginManager:
    """Return the plugin manager for the active Mertina profile/home (cached per resolved home; a
    profile switch gets its own manager and plugin submodules)."""
    global _plugin_manager
    current_home = _plugin_home_key()
    with _plugin_managers_lock:
        # Tests/embedders monkeypatch ``_plugin_manager`` directly: adopt a single-slot manager the
        # keyed cache doesn't know about at all.
        if _plugin_manager is not None and _plugin_manager not in _plugin_managers_by_home.values():
            _plugin_managers_by_home[current_home] = _plugin_manager
            return _plugin_manager
        manager = _plugin_managers_by_home.get(current_home)
        if manager is None:
            manager = PluginManager(scope_key=mertina_home_key(current_home))
            _plugin_managers_by_home[current_home] = manager
        _plugin_manager = manager
        return manager


def _ensure_plugins_discovered(force: bool = False) -> PluginManager:
    """Return the global manager after idempotent (or ``force``d) discovery."""
    manager = get_plugin_manager()
    manager.discover_and_load(force=force)
    return manager
