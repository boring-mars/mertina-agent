# Ported from hermes-agent tools/web_tools.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
#!/usr/bin/env python3
"""Generic web_search tool over pluggable backends.

Backend is selected during ``mertina tools`` (``web.backend`` in config.yaml; per
capability via ``web.search_backend``). Every vendor
implementation lives in ``plugins/web/<vendor>/provider.py`` and registers with
``agent.web_search_registry``; this module owns selection.
"""

import json
import logging

from mertina.tools.tool_backend_helpers import (
    NOUS_MANAGED_PROVIDER,
    read_selection,
    selection_exists,
)
from mertina.tools.web_tools_extract import (
    _no_provider_error,
    _strict_selection_error,
)

logger = logging.getLogger(__name__)


# ─── Backend Selection ────────────────────────────────────────────────────────


def _load_web_config() -> dict:
    """Load the ``web:`` section from config.yaml; always a dict (a null section yields ``{}``)."""
    try:
        from mertina.cli.config import load_config

        return load_config().get("web") or {}
    except Exception:
        return {}


def _configured_backend(key: str = "backend") -> str:
    """Lower-cased, stripped ``web.<key>`` value ("" when unset/null)."""
    return (_load_web_config().get(key) or "").lower().strip()


def _registry_call(func_name: str, default, *args):
    """``agent.web_search_registry.<func_name>(*args)``, or *default* if it raised (registry never fatal)."""
    try:
        from mertina.agent import web_search_registry as registry_mod

        return getattr(registry_mod, func_name)(*args)
    except Exception as exc:
        logger.debug("web provider registry %s%r failed: %s", func_name, args, exc)
        return default


def _registered_web_provider(backend: str):
    """Plugin-registered web provider by name, or ``None``."""
    return _registry_call("get_provider", None, backend) if backend else None


def _list_registered_web_providers():
    """All plugin-registered web providers (empty list on failure)."""
    return _registry_call("list_providers", [])


def _probe(provider, method: str, context: str = "") -> bool | None:
    """``bool(provider.<method>())``, or ``None`` if it raised (a broken provider is unavailable; *context* is
    appended to the debug log line, e.g. " during readiness check")."""
    try:
        return bool(getattr(provider, method)())
    except Exception as exc:
        name = getattr(provider, "name", provider)
        logger.debug("web provider %r.%s() raised%s: %s", name, method, context, exc)
        return None


def _get_backend() -> str:
    """Shared web backend name. A stored ``web.backend`` is returned as-is — no availability probe, no
    fallback — so a broken selection surfaces the vendor's honest error rather than silently rerouting.
    The managed ``use_gateway`` selection also resolves to firecrawl with no ladder. Autodetect runs
    whenever no SHARED web selection was ever stored: per-capability keys (``web.search_backend``,
    ``web.extract_backend``) name only their own capability and never reroute the other (#113017)."""
    configured = _configured_backend()
    if configured:
        # "nous" (managed subscription) is serviced by firecrawl, routed through the managed Tool Gateway.
        return "firecrawl" if configured == NOUS_MANAGED_PROVIDER else configured
    if read_selection("web") is not None:
        # Shared selection exists (use_gateway) but no shared name: firecrawl, no ladder.
        return "firecrawl"

    # Never-configured install.
    backend_candidates = (("ddgs", _ddgs_package_importable()),)
    for backend, available in backend_candidates:
        if available:
            return backend

    # Plugin-contributed providers (built-ins are covered above); probe the held object directly.
    for provider in _list_registered_web_providers():
        if provider.name not in _LEGACY_WEB_BACKENDS and _probe(provider, "is_available"):
            return provider.name

    return "firecrawl"  # default (backward compat)


def _get_search_backend() -> str:
    """Backend for web_search: ``web.search_backend`` (strict, no probe) > ``web.backend`` > autodetect."""
    return _configured_backend("search_backend") or _get_backend()


def _ddgs_package_importable() -> bool:
    """ddgs is the only backend gated on package presence; single symbol so tests can patch it."""
    try:
        import ddgs  # noqa: F401

        return True
    except ImportError:
        return False


# Built-in backends -> cheap availability probes; any other name is a plugin provider resolved via the
# registry's ``is_available()``. Lambdas so test patches of module-level helpers (_ddgs_package_importable)
# are honored at call time.
_BUILTIN_AVAILABILITY = {
    "ddgs": lambda: _ddgs_package_importable(),
}
_LEGACY_WEB_BACKENDS = frozenset(_BUILTIN_AVAILABILITY)


def _is_backend_available(backend: str) -> bool:
    """True when *backend* is usable — the single availability chokepoint. Non-legacy names delegate to the
    registered provider's ``is_available()`` (unregistered names fall through); built-ins use cheap probes.

    For plugin-registered backends (any name outside :data:`_LEGACY_WEB_BACKENDS`), availability is
    delegated to the provider's ``is_available()`` via the web_search_registry. This is the single
    chokepoint through which ``_get_backend``, ``_get_capability_backend``, and ``check_web_api_key`` all
    resolve availability — fixing custom-provider discovery for every caller at once (issues #28651, #31873,
    #32698). Built-in backends keep their cheap hardcoded probes below.
    """
    backend = (backend or "").lower().strip()
    provider = None if backend in _LEGACY_WEB_BACKENDS else _registered_web_provider(backend)
    if provider is not None:
        return _probe(provider, "is_available") or False
    probe = _BUILTIN_AVAILABILITY.get(backend)
    return probe() if probe else False


# ─── Dispatch ─────────────────────────────────────────────────────────────────


def _ensure_web_plugins_loaded() -> None:
    """Idempotently run plugin discovery so the web registry is populated. Dispatch is reachable from contexts
    that never triggered discovery (subprocess agent runs, delegate children, scripts); without it a
    configured backend yields a misleading "No web ... provider" error.

    Every bundled web provider (brave-free, ddgs, searxng, exa, parallel, tavily, firecrawl, keenable)
    registers itself via ``plugins/web/<vendor>/__init__.py`` during plugin discovery. Tool dispatch can be
    reached from contexts that haven't already triggered discovery — subprocess agent runs, delegate
    children, standalone scripts, certain test paths — and without it the registry is empty and
    ``get_provider('firecrawl')`` returns ``None`` even when the user has ``web.extract_backend: firecrawl``
    configured and ``FIRECRAWL_API_KEY`` set. See #27580.
    """
    try:
        from mertina.cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
    except Exception as exc:
        # Warning, not debug: a broken plugin import is otherwise invisible.
        logger.warning("Web plugin discovery failed (non-fatal): %s", exc)


def web_search_tool(query: str, limit: int = 5) -> str:
    """Search the web via the configured backend.

    Returns a JSON string ``{"success": bool, "data": {"web": [{"title", "url", "description", "position"},
    ...]}}`` (metadata only) or ``{"success": false, "error": ...}``.
    """
    try:
        limit = min(max(int(limit), 1), 100)
    except (TypeError, ValueError):
        limit = 5
    try:
        from mertina.tools.interrupt import is_interrupted

        if is_interrupted():
            return tool_error("Interrupted", success=False)
        # Sync only — every provider's search() is sync.
        _ensure_web_plugins_loaded()
        from mertina.agent.web_search_registry import get_active_search_provider
        from mertina.agent.web_search_registry import get_provider as _wsp_get_provider

        backend = _get_search_backend()
        provider = _wsp_get_provider(backend) if backend else None
        if provider is None or not provider.supports_search():
            if provider is None and backend and selection_exists("web"):
                error_text = _strict_selection_error("search", backend)
                return json.dumps(
                    {"success": False, "error": error_text}, indent=2, ensure_ascii=False
                )
            # Never-configured install: legacy availability-walked autodetect.
            provider = get_active_search_provider()

        if provider is None:
            fallback = "No web search provider configured. Run `mertina tools` to set one up."
            response_data = {"success": False, "error": _no_provider_error("search", fallback)}
        else:
            logger.info("Web search via %s: '%s' (limit: %d)", provider.name, query, limit)
            response_data = provider.search(query, limit)

        result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
        return result_json
    except Exception as e:
        return tool_error(f"Error searching web: {e!s}")


def _provider_is_ready(provider) -> bool:
    """True when *provider* is available, without raising.

    ``get_active_*_provider()`` returns an explicitly configured backend even when ``is_available()`` is
    False (so dispatch can emit a precise error), so readiness gates (tool check_fn, ``mertina doctor``)
    must probe for real.

    See #78412.
    """
    if provider is None:
        return False
    ready = _probe(provider, "is_available", " during readiness check")
    if ready is None:  # broken provider == not ready
        return False
    return bool(ready)


# Credential probes that back other tools but serve no registered web backend: ``xai`` is
# probed via has_xai_credentials() for TTS/media only, so it must not light this gate. A
# stored ``web.backend: xai`` still counts since _get_backend returns a configured
# selection as-is and dispatch surfaces the honest "unknown provider" error.
_WEB_CHECK_SKIP = frozenset({"xai"})


def check_web_api_key() -> bool:
    """``check_fn`` gate for web_search: is any web backend available?

    A plugin-registered provider reporting ``is_available()`` must light the tools up even with no
    built-in credentials; resolution funnels through :func:`_is_backend_available`.

    See #28651, #31873.
    """
    # Boolean OR over configured + built-ins — probe order is irrelevant here.
    candidates = [c for c in (_configured_backend(),) if c] + [
        b for b in _LEGACY_WEB_BACKENDS if b not in _WEB_CHECK_SKIP
    ]
    if any(_is_backend_available(backend) for backend in candidates):
        return True
    # Plugin path. Discovery must run first: check_fn fires at tool-registration time, before any dispatch.
    try:
        _ensure_web_plugins_loaded()
        from mertina.agent.web_search_registry import (
            get_active_search_provider,
        )

        for provider in (get_active_search_provider(),):
            if _provider_is_ready(provider):
                return True
        return False
    except Exception as exc:
        logger.debug("web provider registry availability check failed: %s", exc)
        return False


# ─── Registry ─────────────────────────────────────────────────────────────────
from mertina.tools.registry import registry, tool_error

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": 'Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. The query is passed through to the configured backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, and "exact phrase" may work when the backend supports them.',
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": 'The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or "exact phrase".',
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=args.get("limit", 5)),
    check_fn=check_web_api_key,
    emoji="🔍",
    max_result_size_chars=100_000,
)
