# Ported from hermes-agent hermes_cli/plugins_loader.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Plugin loading: directory/entry-point module import, deferred bundled platforms, portable packages,
dependency/config-schema warnings. Mixed into :class:`mertina.cli.plugins.PluginManager`.

Origin-internal names (``PluginContext``, ``LoadedPlugin``, ``_PLUGINS_DEBUG`` …) are imported lazily
through ``mertina.cli.plugins`` so tests that patch them on the origin keep working.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import sys
import threading
import types
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from mertina.cli.plugins_manifest import PluginManifest, manifest_key
from mertina.constants import (
    reset_mertina_home_override,
    set_mertina_home_override,
)
from mertina.registration_lifecycle import replacement_coordinator

logger = logging.getLogger("mertina.cli.plugins")


_NS_PARENT = "mertina_plugins"


_MODULE_NAMESPACE_LOCK = threading.RLock()


_BARE_MODULE_SCOPE: dict[str, str] = {}  # bare module name -> owning scope_key


def _evict_modules(module_name: str) -> None:
    """Drop ``module_name`` and every ``module_name.*`` submodule from ``sys.modules``."""
    prefix = f"{module_name}."
    for name in [n for n in sys.modules if n == module_name or n.startswith(prefix)]:
        del sys.modules[name]


def _serialized_replacement(method):
    """Make snapshot → write → lease attachment one atomic transaction."""

    @wraps(method)
    def wrapped(*args, **kwargs):
        with replacement_coordinator.transaction():
            return method(*args, **kwargs)

    return wrapped


@contextmanager
def _plugin_home_scope(home: Path):
    """Bind discovery and loading to the manager's immutable Mertina home."""
    token = set_mertina_home_override(home)
    try:
        yield
    finally:
        reset_mertina_home_override(token)


class PluginLoaderMixin:
    @staticmethod
    def _load_plugin(self, manifest: PluginManifest) -> None:
        """Import a plugin module and call its ``register(ctx)`` function."""
        with self._discovery_lock, _plugin_home_scope(self.home_path):
            self._load_plugin_scoped(manifest)

    def _load_plugin_scoped(self, manifest: PluginManifest) -> None:
        """Load one plugin with the manager's home bound as current."""
        from mertina.cli.plugins import _PLUGINS_DEBUG, LoadedPlugin, PluginContext

        loaded = LoadedPlugin(manifest=manifest)
        plugin_key = manifest_key(manifest)
        logger.debug(
            "Loading plugin '%s' (source=%s, kind=%s, path=%s)",
            plugin_key,
            manifest.source,
            manifest.kind,
            manifest.path,
        )
        if manifest.portable:
            self._load_portable_plugin(manifest, loaded)
            return
        # requires_mertina gate: skip cleanly (no import, no traceback) on a version mismatch.
        from mertina.cli.plugins_manifest import requires_mertina_error

        reason = requires_mertina_error(manifest)
        if reason:
            loaded.error = reason
            logger.warning("Plugin '%s' skipped: %s", plugin_key, reason)
            self._plugins[plugin_key] = loaded
            return
        # After the compat-removal date an external plugin that still imports pre-decomposition paths is
        # skipped with a clear reason instead of dying on ImportError mid-register (mertina.cli.plugin_compat).
        from mertina.cli.plugin_compat import disable_reason

        reason = disable_reason(manifest)
        if reason:
            loaded.error = reason
            logger.warning("Plugin '%s' not loaded: %s", manifest.name, reason)
            self._plugins[plugin_key] = loaded
            return
        registration_start = len(self._registration_order)
        module_name = self._policy_module_name(manifest)
        self._track_tool_override_policy(manifest, module_name)
        try:
            # Reuse a deferred platform's already-imported package so its body doesn't run twice.
            # See #78050.
            module = self._predeclared_modules.pop(plugin_key, None)
            if module is None and manifest.source in {"user", "project", "bundled"}:
                module = self._load_directory_module(manifest, module_name=module_name)
            elif module is None:
                module = self._load_entrypoint_module(manifest)
            loaded.module = module
            register_fn = getattr(module, "register", None)
            if register_fn is None:
                loaded.error = "no register() function"
                logger.warning("Plugin '%s' has no register() function", manifest.name)
            else:
                register_fn(PluginContext(manifest, self))
                self._attribute_registrations(loaded, plugin_key, registration_start)
                loaded.enabled = True
                from mertina.cli.plugins_ledger import _hook_source_of

                self._drop_fallback_hooks(_hook_source_of(manifest.name, module))
        except Exception as exc:
            owned = [r for r in self._registration_order if r.plugin_key == plugin_key]
            self._dispose_registrations(owned)
            self._forget_registrations(owned)
            loaded.error = str(exc)
            # register() may have subscribed before raising; a failed plugin must leave no callable reachable
            # from later event dispatch.
            self._remove_plugin_subscriptions(plugin_key)
            logger.warning(
                "Failed to load plugin '%s': %s", manifest.name, exc, exc_info=_PLUGINS_DEBUG
            )
        # The failure path swept this plugin's whole ledger (not just the registration_start slice), so
        # discovery-time pre-registrations are gone too.
        # There is no live tool left to credit — attribution and the registry agree at zero. Only the
        # success path pops _predeclared_tools, so drop the entry here rather than let the bookkeeping
        # outlive the load attempt (#78050).
        if not loaded.enabled:
            self._predeclared_tools.pop(plugin_key, None)
        self._plugins[plugin_key] = loaded

    def _load_directory_module(
        self,
        manifest: PluginManifest,
        *,
        module_name: str | None = None,
    ) -> types.ModuleType:
        """Import a directory plugin as ``mertina_plugins.<slug>`` (slug from ``manifest.key`` so
        ``image_gen/openai`` cannot collide with ``tts/openai``)."""
        plugin_dir = Path(manifest.path)  # type: ignore[arg-type]
        init_file = plugin_dir / "__init__.py"
        if not init_file.exists():
            raise FileNotFoundError(f"No __init__.py in {plugin_dir}")
        if _NS_PARENT not in sys.modules:
            ns_pkg = types.ModuleType(_NS_PARENT)
            ns_pkg.__path__ = []  # type: ignore[attr-defined]
            ns_pkg.__package__ = _NS_PARENT
            sys.modules[_NS_PARENT] = ns_pkg
        module_name = module_name or self._directory_module_name(manifest)
        # Evict stale entries for this slug (same slug cached from another Mertina home, or an earlier force
        # reload). Replacing only sys.modules[module_name] is not enough: the plugin's relative imports are
        # cached as "module_name.sub" and resolve from sys.modules first, so a stale submodule would keep
        # serving the previous load's code/state.
        _evict_modules(module_name)
        spec = importlib.util.spec_from_file_location(
            module_name, init_file, submodule_search_locations=[str(plugin_dir)]
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create module spec for {init_file}")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = module_name
        module.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            # Don't leave a half-initialized module (or its partially imported relative submodules) cached — a
            # retry or a same-slug plugin in another profile would inherit broken state.
            _evict_modules(module_name)
            raise
        return module
