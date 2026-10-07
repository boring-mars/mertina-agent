# Ported from hermes-agent agent/web_search_registry.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Web Search Provider Registry.

Populated by plugins via :meth:`PluginContext.register_web_search_provider`;
consumed by the ``web_search`` / ``web_extract`` wrappers in :mod:`tools.web_tools`.

Active selection, in precedence order (the ``supports_search`` /
``supports_extract`` capability filter applies at every step, so a search-only
provider configured as ``web.extract_backend`` falls through):

1. ``web.search_backend`` / ``web.extract_backend``, then ``web.backend``.
2. The single capability-eligible provider that is registered AND available.
3. Legacy preference walk (``_LEGACY_PREFERENCE``) filtered by availability —
   the historic ``tools.web_tools._get_backend()`` order, so installs that never
   set a config key keep landing on the same provider.
4. Keyless free-tier walk (``_KEYLESS_PREFERENCE``), last resort.
5. ``None`` — the tool points the user at ``mertina tools``.
"""

from __future__ import annotations

import logging

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

        cur = load_config_readonly()
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


# Paid providers first so existing paid setups don't get downgraded to a free
# tier on upgrade; filtered by ``is_available()`` at walk time.
_LEGACY_PREFERENCE = (
    "firecrawl",
    "parallel",
    "tavily",
    "perplexity",
    "exa",
    "searxng",
    "brave-free",
    "ddgs",
)


# Anonymous public free tiers (see plugins/web/keyless_mcp.py); strictly last
# resort, i.e. zero web credentials and no importable ddgs. Unpinned keyless
# traffic round-robins across the ring per request; an explicit `mertina tools`
# pick bypasses this walk. Disable with ``web.keyless_fallback: false``.
_KEYLESS_PREFERENCE = ("exa", "parallel", "firecrawl", "keenable")


def _keyless_preference() -> tuple:
    """Keyless walk order, starting at the ring cursor in
    :mod:`plugins.web.keyless_mcp` so resolution and dispatch agree on which
    vendor a fresh install starts at; the rest follow in ring order."""
    try:
        from mertina.plugins.web.keyless_mcp import _KEYLESS_RING, _ring_cursor

        start = _ring_cursor % len(_KEYLESS_RING)
        return tuple(_KEYLESS_RING[start:] + _KEYLESS_RING[:start])
    except Exception as exc:
        logger.debug("keyless ring order unavailable: %s", exc)
    return _KEYLESS_PREFERENCE


def _resolve(configured: str | None, *, capability: str) -> WebSearchProvider | None:
    """Resolve the active provider for a capability ("search" | "extract").

    Rules, in order (see module docstring): explicit config wins even when
    ``is_available()`` is False (the dispatcher surfaces a precise
    "X_API_KEY is not set" error instead of a silent switch); then the single
    available capable provider; then the availability-filtered legacy walk;
    then the keyless free-tier walk; else None.
    """
    snapshot = _registry.merged()

    def _capable(p: WebSearchProvider) -> bool:
        return bool(
            p.supports_search()
            if capability == "search"
            else p.supports_extract()
            if capability == "extract"
            else False
        )

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

    # Fallbacks are availability-filtered so a registered-but-keyless provider
    # never becomes "active" on a fresh install.
    eligible = [p for p in snapshot.values() if _capable(p) and _available(p)]
    if len(eligible) == 1:
        return eligible[0]

    for legacy in _LEGACY_PREFERENCE:
        provider = snapshot.get(legacy)
        if provider is not None and provider in eligible:
            return provider

    # Keyless free tier (anonymous public MCP tiers) is last-resort only: it is
    # reachable solely when the legacy walk found nothing, never pre-empting a
    # keyed setup. Disabled via ``web.keyless_fallback: false``.
    if _keyless_tier_enabled():
        for name in _keyless_preference():
            provider = snapshot.get(name)
            if provider is None or not _capable(provider):
                continue
            try:
                if provider.is_keyless_available():
                    return provider
            except Exception as exc:
                logger.debug("provider %s.is_keyless_available() raised %s", name, exc)

    return None


def _keyless_tier_enabled() -> bool:
    """Read ``web.keyless_fallback`` from config.yaml (default: enabled)."""
    try:
        from mertina.cli.config import load_config

        web_cfg = load_config().get("web") or {}
        return bool(web_cfg.get("keyless_fallback", True))
    except Exception as exc:
        logger.debug("keyless_fallback config read failed: %s", exc)
        return True


def get_active_search_provider() -> WebSearchProvider | None:
    """Resolve the currently-active web search provider."""
    return _resolve(_configured_backend("search"), capability="search")


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import threading  # noqa: F401,E402
from typing import (
    Dict,  # noqa: F401
    List,  # noqa: F401
)
