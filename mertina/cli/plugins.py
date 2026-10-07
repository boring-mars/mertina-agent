# Ported from hermes-agent hermes_cli/plugins.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Mertina Plugin System — discovers, loads, and manages plugins.

Sources, later overriding earlier on key collision: bundled ``<repo>/plugins/<name>/`` (``memory/``
and ``context_engine/`` have their own discovery), user ``~/.mertina/plugins/<name>/``, project
``./.mertina/plugins/<name>/`` (opt-in via ``MERTINA_ENABLE_PROJECT_PLUGINS``), and pip packages in
the ``mertina_agent.plugins`` entry-point group. A directory plugin needs a ``plugin.yaml`` manifest
and an ``__init__.py`` exposing ``register(ctx)``. Plugins register callbacks for ``VALID_HOOKS``
(core fires ``invoke_hook(name, **kwargs)``) and tools via ``PluginContext.register_tool()``.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import queue
import sys
import threading
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mertina.cli.plugins_discovery import (  # noqa: F401 — re-exported
    ENTRY_POINTS_GROUP,
    _get_disabled_plugins,
    _get_enabled_plugins,
    collect_directory_manifests,
    discover_entrypoint_manifests,
    gate_manifest,
    scan_directory,
)
from mertina.cli.plugins_dispatch import (  # noqa: F401 — re-exported
    _EVENT_EMIT_DEPTH_CAP,
    _EVENT_PENDING_CAP,
    _HOOK_CALLBACK_TIMEOUT_SECS,
    _HOOK_TIMEOUT_SUPPRESSION_SECONDS,
    _MAX_HOOK_CALLBACK_TIMEOUT_SECS,
    _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE,
    DEFAULT_SYSTEM_PROMPT_SECTION_MAX_CHARS,
    MAX_SYSTEM_PROMPT_SECTION_CHARS,
    MAX_SYSTEM_PROMPT_SECTIONS_TOTAL_CHARS,
    MERTINA_EVENT_NAMESPACE,
    PLUGIN_SECTIONS_END,
    PLUGIN_SECTIONS_START,
    SYSTEM_PROMPT_SECTION_POSITIONS,
    PluginDispatchMixin,
    PluginSystemPromptSection,
    RenderedPluginSystemPromptSection,
    _EventSubscription,
    format_system_prompt_sections,
    is_valid_system_prompt_section_id,
)
from mertina.cli.plugins_ledger import PluginLedgerMixin, PluginRegistration
from mertina.cli.plugins_loader import (
    PluginLoaderMixin,
    _plugin_home_scope,
    _serialized_replacement,
)

# Sibling modules' names are re-exported here (origin) so plugins and tests keep one import path.
from mertina.cli.plugins_manifest import (  # noqa: F401 — re-exported
    _CONFIG_SCHEMA_TYPES,
    SUPPORTED_MANIFEST_VERSION,
    PluginManifest,
    _portable_skill_namespace,
    manifest_key,
    parse_manifest_file,
    resolve_module_origin,
    resolve_plugin_load_order,
    validate_config_schema,
)
from mertina.cli.relay_plugin_cutover import RELAY_PLUGINS_CONFIG_ENV, legacy_relay_plugin_keys
from mertina.constants import get_mertina_home, mertina_home_key
from mertina.utils import env_var_enabled


def get_bundled_plugins_dir() -> Path:
    """Bundled ``plugins/`` dir: ``MERTINA_BUNDLED_PLUGINS`` (Nix wrapper / packaged installs, read-only
    store paths) first, else the in-repo path."""
    env_override = os.getenv("MERTINA_BUNDLED_PLUGINS")
    if env_override:
        return Path(env_override)
    return Path(__file__).resolve().parent.parent / "plugins"


logger = logging.getLogger(__name__)


# ``MERTINA_PLUGINS_DEBUG=1`` tees verbose discovery logs to stderr in addition to agent.log. Read
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


_env_enabled = env_var_enabled  # imported by plugins/memory


@dataclass
class LoadedPlugin:
    """Runtime state for a single loaded plugin."""

    manifest: PluginManifest
    module: types.ModuleType | None = None
    tools_registered: list[str] = field(default_factory=list)
    hooks_registered: list[str] = field(default_factory=list)
    middleware_registered: list[str] = field(default_factory=list)
    commands_registered: list[str] = field(default_factory=list)
    enabled: bool = False
    error: str | None = None
    # Bundled platform recorded as a not-yet-imported loader (see _register_deferred_platform).
    deferred: bool = False


class PluginContext:
    """Facade given to plugins so they can register tools and hooks."""

    def __init__(self, manifest: PluginManifest, manager: PluginManager):
        self.manifest = manifest
        self._manager = manager
        self._llm: Any = None  # lazy; tests preseed it (see ``llm``)

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
        register in the scope-keyed ``registry``, lease the slot so unload restores the displaced entry.
        ``None`` when the registry refused/replaced the provider (``ValueError`` with ``reject_message``
        set, or a falsy ``register``)."""
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

    _MCP_RESULT_CHAR_CAP = 65536


# -- scoped provider registrars ------------------------------------------------------------------
# Every ``register_<category>_provider`` shares one body (:meth:`PluginContext._register_scoped_provider`):
# type-check, register in the scope-keyed process-global registry, lease the slot so unload restores
# the displaced entry. Rows: (method, kind, registry module, base-class module:attr, label, docstring,
# options). ``normalize``: ``strip`` (default), ``lower`` (strip+lowercase) or ``None`` (raw name).
_SCOPED_PROVIDER_REGISTRARS: tuple[tuple[str, str, str, str, str, str, dict[str, Any]], ...] = (
    (
        "register_image_gen_provider",
        "image_gen_provider",
        "mertina.agent.image_gen_registry",
        "mertina.agent.image_gen_provider:ImageGenProvider",
        "image_gen provider",
        "Register an :class:`agent.image_gen_provider.ImageGenProvider`; "
        "``provider.name`` is matched by ``image_gen.provider``.",
        {"article": "an"},
    ),
    (
        "register_video_gen_provider",
        "video_gen_provider",
        "mertina.agent.video_gen_registry",
        "mertina.agent.video_gen_provider:VideoGenProvider",
        "video_gen provider",
        "Register an :class:`agent.video_gen_provider.VideoGenProvider`; "
        "``provider.name`` is matched by ``video_gen.provider``.",
        {},
    ),
    (
        "register_web_search_provider",
        "web_search_provider",
        "mertina.agent.web_search_registry",
        "mertina.agent.web_search_provider:WebSearchProvider",
        "web provider",
        "Register an :class:`agent.web_search_provider.WebSearchProvider`; "
        "``provider.name`` is matched by ``web.search_backend`` / ``web.extract_backend`` / ``web.backend``.",
        {},
    ),
    (
        "register_browser_provider",
        "browser_provider",
        "mertina.agent.browser_registry",
        "mertina.agent.browser_provider:BrowserProvider",
        "browser provider",
        "Register an :class:`agent.browser_provider.BrowserProvider`; "
        "``provider.name`` is matched by ``browser.cloud_provider`` (consulted by "
        "``tools.browser_tool_cloud._get_cloud_provider``).",
        {},
    ),
    (
        "register_terminal_environment_provider",
        "terminal_environment_provider",
        "mertina.agent.terminal_env_registry",
        "mertina.agent.terminal_env_provider:TerminalEnvironmentProvider",
        "terminal environment provider",
        "Register a :class:`agent.terminal_env_provider.TerminalEnvironmentProvider`; ``provider.name`` "
        "is matched by ``terminal.backend`` when no built-in backend has that name. Built-in names (local, "
        "docker, singularity, modal, daytona, vercel_sandbox, ssh) are rejected — plugins never shadow "
        "in-tree backends.",
        {
            "normalize": "lower",
            "reject_message": "Plugin '%s' terminal environment provider rejected: %s",
        },
    ),
    (
        "register_secret_source",
        "secret_source",
        "mertina.agent.secret_sources.registry",
        "mertina.agent.secret_sources.base:SecretSource",
        "secret source",
        "Register a :class:`agent.secret_sources.base.SecretSource`, run by ``load_mertina_dotenv()`` "
        "(after ``~/.mertina/.env``, before credentials are read) when ``secrets.<name>`` is enabled. The "
        "orchestrator owns ordering/precedence/provenance; the source only fetches. Since dotenv usually "
        "loads before discovery, the manager re-pulls enabled plugin sources afterwards.",
        {"normalize": None, "register": "register_source", "param": "source"},
    ),
    (
        "register_tts_provider",
        "tts_provider",
        "mertina.agent.tts_registry",
        "mertina.agent.tts_provider:TTSProvider",
        "TTS provider",
        "Register an :class:`agent.tts_provider.TTSProvider`; ``provider.name`` is matched by "
        "``tts.provider`` unless it is a built-in name (rejected with a warning) or a "
        "``tts.providers.<name>: type: command`` entry shares it (command-providers win).",
        {"normalize": "lower"},
    ),
    (
        "register_transcription_provider",
        "transcription_provider",
        "mertina.agent.transcription_registry",
        "mertina.agent.transcription_provider:TranscriptionProvider",
        "transcription provider",
        "Register an :class:`agent.transcription_provider.TranscriptionProvider`; ``provider.name`` is "
        "matched by ``stt.provider`` unless it is a built-in name (rejected) or a ``stt.providers.<name>: "
        "type: command`` entry shares it (command-providers win).",
        {"normalize": "lower"},
    ),
)


_NAME_NORMALIZERS: dict[str | None, Callable[[str], str] | None] = {
    "strip": lambda n: n.strip(),
    "lower": lambda n: n.strip().lower(),
    None: None,
}


def _make_scoped_provider_registrar(method_name, kind, registry_mod, base_ref, label, doc, options):
    """Build one ``register_<category>_provider`` method from a ``_SCOPED_PROVIDER_REGISTRARS`` row."""
    base_mod, base_attr = base_ref.split(":")
    normalize_fn = _NAME_NORMALIZERS[options.get("normalize", "strip")]
    register_name = options.get("register")

    def register(self, provider) -> PluginRegistration | None:
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

    def register_source(self, source) -> PluginRegistration | None:  # secret sources: ``source``
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


class PluginManager(PluginLoaderMixin, PluginDispatchMixin, PluginLedgerMixin):
    """Central manager that discovers, loads, and invokes plugins."""

    def __init__(self, scope_key: str | None = None) -> None:
        # Home is captured immutably: unload may run from another profile context, but every
        # inverse must target the registration's original scope.
        self.scope_key = scope_key or mertina_home_key()
        self.home_path = Path(self.scope_key)
        self._discovery_lock = threading.RLock()
        self._discovered: bool = False
        self._cli_ref = None  # Set by CLI after plugin discovery
        self._gateway_message_injector: tuple[object, Callable] | None = None
        self._context_engine = None  # Set by a plugin via register_context_engine()
        # Manager-local registries keyed by name (see the matching ``PluginContext.register_*``):
        # plugins, hooks, middleware, CLI + slash commands, prompt sections, skills (qualified name ->
        # metadata), portable MCP servers, auxiliary tasks, approval transports, Slack action handlers
        # (matcher, callback, plugin_name), platform handler factories (lowercase platform -> list).
        self._plugins: dict[str, LoadedPlugin] = {}
        self._hooks: dict[str, list[Callable]] = {}
        # Fallback hooks registered by a memory provider before general discovery.
        self._memory_hook_registrations: dict[tuple[str, str], list[PluginRegistration]] = {}
        self._middleware: dict[str, list[Callable]] = {}
        self._plugin_tool_names: set[str] = set()
        self._plugin_platform_names: set[str] = set()
        self._cli_commands: dict[str, dict] = {}
        self._plugin_commands: dict[str, dict] = {}
        self._system_prompt_sections: dict[str, PluginSystemPromptSection] = {}
        self._plugin_skills: dict[str, dict[str, Any]] = {}
        self._portable_mcp_servers: dict[str, dict[str, Any]] = {}
        self._aux_tasks: dict[str, dict[str, Any]] = {}
        self._approval_transports: dict[str, Any] = {}
        self._slack_action_handlers: list[tuple] = []
        self._platform_handler_factories: dict[str, list[tuple]] = {}
        # Event bus: owner-tagged subscriptions (unload removes zombies); one daemon worker keeps
        # registration order while emitters never block; per-worker chain depth caps mutual emitters.
        self._subscriptions: dict[str, list[_EventSubscription]] = {}
        self._event_lock = threading.RLock()
        self._event_idle = threading.Condition(self._event_lock)
        self._event_generation = 0
        self._event_pending_by_generation: dict[int, int] = {0: 0}
        self._event_queue: queue.Queue[Any] = queue.Queue(maxsize=_EVENT_PENDING_CAP)
        self._event_worker: threading.Thread | None = None
        self._emit_depth = threading.local()
        # In-flight / recently-timed-out hook callbacks keyed by (hook_name, id(cb), call_identity)
        # so a stuck policy hook cannot spawn a new abandoned thread on every fire.
        self._hook_running_callbacks: dict[tuple, object] = {}
        self._hook_abandoned: dict[tuple, set] = {}
        self._hook_timeout_suppressed_until: dict[tuple, float] = {}
        self._hook_timeout_lock = threading.Lock()
        self._hook_timeout_suppression_seconds = _HOOK_TIMEOUT_SUPPRESSION_SECONDS
        # (hook_name, id(cb), repr(exc)) already reported at WARNING; identical repeats go to DEBUG.
        self._hook_failures_reported: set = set()
        # Ledger per plugin (ownership) plus global order (reverse teardown across plugins). Process-
        # global registries are shared across profiles while several managers coexist, so the ledger
        # is keyed per (mertina_home, plugin_id) and every inverse is identity-conditional — one
        # profile's unload can never clear another's. Persistent registrations that survived an
        # unload-all park in ``_persistent_carryover`` until force re-discovery evicts the stale ones.
        # Registration handles are kept both per plugin (ownership lookup) and globally (reverse-order
        # teardown for overrides spanning plugins). Registry overlays keyed by scope_key (see
        # tools/registry.py and gateway/platform_registry.py) carry the profile dimension; anything still
        # process-global is guarded by the identity checks. TODO(#64178): extend explicit profile keying to
        # any remaining process-global slots when the symmetric force-reload lands.
        self._ownership_ledger: dict[str, list[PluginRegistration]] = {}
        self._registration_order: list[PluginRegistration] = []
        # Force re-discovery drains this via _evict_stale_persistent_registrations(): entries whose plugin
        # re-registered the same (kind, key) are kept (the upsert rotated them in place), the rest are
        # disposed so a disabled/removed auth plugin's provider does not outlive its plugin (#91701
        # follow-up).
        self._persistent_carryover: list[PluginRegistration] = []
        # Deferred platforms whose client tools registered at discovery (see
        # _register_deferred_platform_tools): imported package (don't re-execute on materialize)
        # and contributed tool names (so `mertina plugins list` still attributes them).
        self._predeclared_modules: dict[str, types.ModuleType] = {}
        self._predeclared_tools: dict[str, list[str]] = {}

    def discover_and_load(self, force: bool = False) -> None:
        """Scan all plugin sources and load each plugin found; ``force`` unloads first so config
        changes / new bundled backends become visible in long-lived sessions."""
        with self._discovery_lock, _plugin_home_scope(self.home_path):
            if self._discovered and not force:
                return
            if force:
                self.unload()  # the ledger owns teardown of process-global registries
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
                # Persistent registrations survived the unload-all; now that plugins re-registered,
                # dispose the ones whose plugin did not come back.
                # Now that plugins have had their chance to re-register, dispose the ones whose plugin did
                # not come back (disabled, removed, or omitted from this discovery pass) so e.g. a disabled
                # auth plugin's provider does not stay live process-wide until restart. See #91701.
                self._evict_stale_persistent_registrations()
                # load_mertina_dotenv() ran at import, before plugin secret sources existed: re-pull.
                # Plugin secret sources register during discover; the initial load_mertina_dotenv() already
                # ran at import time. Re-pull so the first process sees plugin backends (tracking #64177).
                self._refresh_secret_sources_after_discovery()
                if force:
                    # config.yaml shell hooks / outbound webhooks live in ``_hooks`` but are
                    # config-owned; unload() wiped them and cannot restore them.
                    # Re-register so force-reload is symmetric (#60036; tracking #64178 — salvaged from PR
                    # #64188; outbound webhooks added per #92682 review).
                    self._re_register_config_hooks_after_force()
            except BaseException:
                self._discovered = False
                raise

    def _discover_and_load_inner(self) -> None:
        """The actual discovery sweep — see :meth:`discover_and_load`."""
        manifests: list[PluginManifest] = self._collect_directory_manifests()
        # Entry points are separate from the directory scan: the startup MCP probe must not import
        # or register them.
        # An installed directory plugin keeps its identity when its own pip dependency also ships an
        # entry point under the same name (the pyproject wrapper shape): the directory is what the
        # user installed, carries catalog provenance and is what update/remove act on.
        directory_keys = {manifest_key(m) for m in manifests}
        ep_manifests = [
            m for m in self._scan_entry_points() if manifest_key(m) not in directory_keys
        ]
        logger.debug("  entrypoints: %d manifest(s)", len(ep_manifests))
        manifests.extend(ep_manifests)
        disabled = _get_disabled_plugins()
        enabled = _get_enabled_plugins()  # None = opt-in default (nothing enabled)
        stale_relay_keys = legacy_relay_plugin_keys(enabled)
        if stale_relay_keys:
            logger.warning(
                "Removed Mertina plugin %s is still listed in plugins.enabled; "
                "remove it and configure native Relay plugins with %s",
                ", ".join(stale_relay_keys),
                RELAY_PLUGINS_CONFIG_ENV,
            )
        # Later sources win on key collision (project > user > bundled); gate the winners, then
        # load survivors in requires_plugins order (see resolve_plugin_load_order).
        winners = {manifest_key(m): m for m in manifests}
        to_load = {k: m for k, m in winners.items() if self._gate_manifest(m, disabled, enabled)}
        for lookup_key in resolve_plugin_load_order(to_load):
            manifest = to_load[lookup_key]
            self._warn_python_dependencies(manifest)
            self._validate_plugin_config_schema(manifest)
            self._load_plugin(manifest)
        if manifests:
            logger.info(
                "Plugin discovery complete: %d found, %d enabled",
                len(self._plugins),
                sum(1 for p in self._plugins.values() if p.enabled),
            )
        self._refresh_plugin_compat_report(list(to_load.values()))

    def _gate_manifest(
        self,
        manifest: PluginManifest,
        disabled: set[str],
        enabled: set[str] | None,
    ) -> bool:
        """Route one winning manifest per :func:`gate_manifest`: load now, defer, or record as
        skipped (introspection-only placeholder). Returns True only for plugins that go through the
        dependency-ordered load pass."""
        verdict = gate_manifest(manifest, disabled, enabled)
        if verdict.action == "load":
            return True
        if verdict.action == "load_now":
            self._load_plugin(manifest)
        elif verdict.action == "defer":
            self._register_deferred_platform(manifest)
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

    def _scan_entry_points(self) -> list[PluginManifest]:
        """Read installed plugin entry points (see :func:`discover_entrypoint_manifests`)."""
        return discover_entrypoint_manifests()


# Legacy single-slot "current" manager, kept so tests that monkeypatch ``_plugin_manager`` keep
# working — ``get_plugin_manager()`` still reads/writes this name.
_plugin_manager: PluginManager | None = None


# Resolved Mertina home -> PluginManager. A process can switch profiles via
# ``set_mertina_home_override()``; a single slot would leak one profile's plugin/context-engine state
# into another, and keying by resolved home lets a re-entered profile reuse its imported modules.
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


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import copy  # noqa: F401,E402
import hashlib  # noqa: F401,E402
import time  # noqa: F401,E402
from collections.abc import Iterable  # noqa: F401,E402
from contextlib import contextmanager  # noqa: F401,E402
from functools import wraps  # noqa: F401,E402
from typing import Type  # noqa: F401,E402
