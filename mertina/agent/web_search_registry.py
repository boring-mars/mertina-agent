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


def get_active_search_provider() -> WebSearchProvider | None:
    """Resolve the currently-active web search provider."""
    return _resolve(_configured_backend("search"), capability="search")
