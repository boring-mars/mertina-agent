# Ported from hermes-agent agent/web_search_registry.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Web Search Provider Registry.

Populated by plugins via :meth:`PluginContext.register_web_search_provider`;
consumed by the ``web_search`` wrapper in :mod:`mertina.tools.web_tools`.

Active selection, in precedence order (the ``supports_search`` capability filter
applies at every step):

1. ``web.search_backend``, then ``web.backend``.
2. The single capability-eligible provider that is registered AND available.
3. Legacy preference walk (``_LEGACY_PREFERENCE``) filtered by availability.
4. ``None``.
"""

from __future__ import annotations

import logging
from typing import Any

from mertina.agent.provider_registry import ProviderRegistry, is_available_safe
from mertina.agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


_registry: ProviderRegistry[WebSearchProvider] = ProviderRegistry(
    label="Web",
    provider_cls=WebSearchProvider,
    logger=logger,
)


_registry.export(globals())


def _read_config_key(*path: str) -> str | None:
    """Resolve a dotted config key from ``config.yaml``. Returns None on miss."""
    try:
        from mertina.cli.config import load_config_readonly

        cur: Any = load_config_readonly()
        for segment in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(segment)
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
    except Exception as exc:
        logger.debug("Could not read config %s: %s", ".".join(path), exc)
    return None


def _configured_backend(capability: str) -> str | None:
    """``web.<capability>_backend`` (preferred) or ``web.backend`` (shared fallback)."""
    return _read_config_key("web", f"{capability}_backend") or _read_config_key("web", "backend")


# Filtered by ``is_available()`` at walk time.
_LEGACY_PREFERENCE = ("ddgs",)


def _resolve(configured: str | None, *, capability: str) -> WebSearchProvider | None:
    """Resolve the active provider for a capability ("search").

    Rules, in order (see module docstring): explicit config wins even when
    ``is_available()`` is False; then the single available capable provider;
    then the availability-filtered legacy walk; else None.
    """
    snapshot = _registry.merged()

    def _capable(p: WebSearchProvider) -> bool:
        return bool(p.supports_search() if capability == "search" else False)

    def _available(p: WebSearchProvider) -> bool:
        return is_available_safe(p, logger, "provider %s.is_available() raised %s")

    if configured:
        provider = snapshot.get(configured)
        if provider is not None and _capable(provider):
            return provider
        if provider is None:
            logger.debug("web backend '%s' configured but not registered; falling back", configured)
        else:
            logger.debug(
                "web backend '%s' configured but does not support '%s'; falling back",
                configured,
                capability,
            )

    # Fallbacks are availability-filtered.
    eligible = [p for p in snapshot.values() if _capable(p) and _available(p)]
    if len(eligible) == 1:
        return eligible[0]

    for legacy in _LEGACY_PREFERENCE:
        provider = snapshot.get(legacy)
        if provider is not None and provider in eligible:
            return provider

    return None


def _disabled_web_plugin_for(
    configured: str | None = None, *, capability: str | None = None
) -> str | None:
    """Plugin key of a *disabled* bundled web plugin that would have provided
    the configured backend (``web.<capability>_backend`` → ``web.backend``), or None.

    Lets the dispatcher say "re-enable web-firecrawl" instead of "No web extract
    provider configured". Resolved from config.yaml rather than the resolved
    backend because a disabled provider fails the availability gate and silently
    drops to the default. Bundled web plugins live under ``web/<vendor>`` with
    the provider name differing only by hyphen/underscore, so both are normalized.

    When a user sets ``web.extract_backend: firecrawl`` (or the search equivalent) but also lists
    ``web-firecrawl`` in ``plugins.disabled``, the provider never registers and the dispatcher would
    otherwise emit a misleading "No web extract provider configured. Set web.extract_backend to ..." error —
    even though the backend IS configured correctly. This helper detects that case so the dispatcher can
    point the user at the actual cause (issue #40190 follow-up: pi314's disabled-plugin symptom).
    """

    def _norm(s: str) -> str:
        return s.strip().lower().replace("-", "_")

    if not configured and capability in ("search", "extract"):
        configured = _configured_backend(capability)
    if not configured:
        return None

    want = _norm(configured)
    try:
        from mertina.cli.plugins import get_plugin_manager

        pm = get_plugin_manager()
        for key, loaded in pm._plugins.items():
            if (
                isinstance(key, str)
                and key.startswith("web/")
                and not loaded.enabled
                and loaded.error == "disabled via config"
                and _norm(key.split("/", 1)[1]) == want
            ):
                return key
    except Exception as exc:
        logger.debug("disabled-web-plugin lookup failed: %s", exc)
    return None


def get_active_search_provider() -> WebSearchProvider | None:
    """Resolve the currently-active web search provider."""
    return _resolve(_configured_backend("search"), capability="search")
