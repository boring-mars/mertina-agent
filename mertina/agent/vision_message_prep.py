# Ported from hermes-agent agent/vision_message_prep.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Transport lookup and tool-result content for ``AIAgent`` API messages (upstream's home for
image-part handling, which v0.1 does not port).
"""

from typing import Any


class VisionMessagePrepMixin:
    """Transport lookup and tool-result content for outgoing messages (see module docstring)."""

    def _get_transport(self, api_mode: str = None):
        """Return the cached transport for the given (or current) api_mode (lazy; None if unregistered)."""
        mode = api_mode or self.api_mode
        cache = getattr(self, "_transport_cache", None)
        if cache is None:
            cache = self._transport_cache = {}
        if cache.get(mode) is None:
            from mertina.agent.transports import get_transport

            cache[mode] = get_transport(mode)
        return cache[mode]

    def _tool_result_content_for_active_model(self, tool_name: str, result: Any) -> Any:
        """Tool message content that is safe for the active model."""
        return result
