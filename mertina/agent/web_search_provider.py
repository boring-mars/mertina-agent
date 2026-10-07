# Ported from hermes-agent agent/web_search_provider.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Web Search Provider ABC.

The single plugin-facing surface every web provider implements; registered via
``PluginContext.register_web_search_provider()`` and selected by
``web.search_backend`` / ``web.backend``.

Response shapes (legacy contract, the tool wrapper does not translate)::

    search:  {"success": True,
              "data": {"web": [{"title", "url", "description", "position"}, ...]}}
    failure: {"success": False, "error": str}
"""

from __future__ import annotations

import abc
from typing import Any

from mertina.agent.provider_base import ProviderBase


class WebSearchProvider(ProviderBase):
    """Abstract base class for a web search backend: implement :meth:`is_available` and
    :meth:`search`."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """True when this provider can service calls. Cheap check only (env var, importable
        dep, instance URL) — NO network; runs at tool registration."""

    def supports_search(self) -> bool:
        """True if this provider implements :meth:`search`."""
        return True

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a web search. Callers gate on :meth:`supports_search`."""
        raise NotImplementedError(f"{self.name} does not support search (override supports_search)")
