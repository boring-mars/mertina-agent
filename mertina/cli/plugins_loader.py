# Ported from hermes-agent hermes_cli/plugins_loader.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Plugin loading: directory module import. Mixed into :class:`mertina.cli.plugins.PluginManager`.

Origin-internal names (``PluginContext``, ``LoadedPlugin``, ``_PLUGINS_DEBUG`` …) are imported
lazily through ``mertina.cli.plugins`` so tests that patch them on the origin keep working."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import logging
import sys
import threading
import types
from functools import wraps
from pathlib import Path

from mertina.cli.plugins_manifest import PluginManifest, manifest_key
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


class PluginLoaderMixin:
    def _load_plugin(self, manifest: PluginManifest) -> None:
        """Import a plugin module and call its ``register(ctx)`` function."""
        with self._discovery_lock:
            self._load_plugin_scoped(manifest)

    def _load_plugin_scoped(self, manifest: PluginManifest) -> None:
        """Load one plugin."""
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
        try:
            if manifest.source in {"user", "project", "bundled"}:
                module = self._load_directory_module(manifest)
            loaded.module = module
            register_fn = getattr(module, "register", None)
            if register_fn is None:
                loaded.error = "no register() function"
                logger.warning("Plugin '%s' has no register() function", manifest.name)
            else:
                register_fn(PluginContext(manifest, self))
                loaded.enabled = True
        except Exception as exc:
            loaded.error = str(exc)
            logger.warning(
                "Failed to load plugin '%s': %s", manifest.name, exc, exc_info=_PLUGINS_DEBUG
            )
        self._plugins[plugin_key] = loaded

    def _directory_module_name(self, manifest: PluginManifest) -> str:
        """Profile-safe import namespace for a directory plugin: the bare ``mertina_plugins.<slug>``
        for the first scope that claims it, a ``__home_<digest>`` suffix for any other scope."""
        slug = manifest_key(manifest).replace("/", "__").replace("-", "_")
        bare_name = f"{_NS_PARENT}.{slug}"
        with _MODULE_NAMESPACE_LOCK:
            if _BARE_MODULE_SCOPE.setdefault(bare_name, self.scope_key) == self.scope_key:
                return bare_name
            digest = hashlib.sha256(self.scope_key.encode("utf-8")).hexdigest()[:12]
            return f"{bare_name}__home_{digest}"

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
        # Evict stale entries for this slug (an earlier load of the same slug). Replacing only
        # sys.modules[module_name] is not enough: the plugin's relative imports are cached as
        # "module_name.sub" and resolve from sys.modules first, so a stale submodule would keep
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
            # Don't leave a half-initialized module (or its partially imported relative submodules)
            # cached — a retry or a same-slug plugin in another profile would inherit broken state.
            _evict_modules(module_name)
            raise
        return module
