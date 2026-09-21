# Ported from hermes-agent agent/client_lifecycle.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Wire-client lifecycle for ``AIAgent``.

``ClientLifecycleMixin`` owns the shared primary client.
"""

import logging
import threading
from typing import Any

from mertina.agent.lazy_forward import forward as _forward

logger = logging.getLogger(
    "mertina.run_agent"
)  # origin module's logger name: log records / caplog filters unchanged


class ClientLifecycleMixin:
    # Set by AIAgent; declared here so the mixin type-checks on its own.
    client: Any
    _client_kwargs: dict[str, Any]

    def _client_log_context(self) -> str:
        thread = threading.current_thread()
        return (
            f"thread={thread.name}:{thread.ident} provider={getattr(self, 'provider', 'unknown')} "
            f"base_url={getattr(self, 'base_url', 'unknown')} "
            f"model={getattr(self, 'model', 'unknown')}"
        )

    def _openai_client_lock(self) -> threading.RLock:
        if getattr(self, "_client_lock", None) is None:
            self._client_lock = threading.RLock()
        return self._client_lock

    @staticmethod
    def _is_openai_client_closed(client: Any) -> bool:
        """Check if an OpenAI client is closed.

        Handles both property and method forms of is_closed:
        - httpx.Client.is_closed is a bool property
        - openai.OpenAI.is_closed is a method returning bool

        Prior bug: getattr(client, "is_closed", False) returned the bound method,
        which is always truthy, causing unnecessary client recreation on every call.
        """
        is_closed_attr = getattr(client, "is_closed", None)
        if is_closed_attr is not None:
            # Handle method (openai SDK) vs property (httpx)
            if callable(is_closed_attr):
                if is_closed_attr():
                    return True
            elif bool(is_closed_attr):
                return True

        http_client = getattr(client, "_client", None)
        if http_client is not None:
            return bool(getattr(http_client, "is_closed", False))
        return False

    _create_openai_client = _forward("mertina.agent.agent_runtime_helpers", "create_openai_client")

    def _close_openai_client(self, client: Any, *, reason: str, shared: bool) -> None:
        if client is None:
            return
        ctx = self._client_log_context()
        try:
            client.close()
            logger.info(
                "OpenAI client closed (%s, shared=%s) %s",
                reason,
                shared,
                ctx,
            )
        except Exception as exc:
            logger.debug(
                "OpenAI client close failed (%s, shared=%s) %s error=%s", reason, shared, ctx, exc
            )

    def _ensure_primary_openai_client(self, *, reason: str) -> Any:
        with self._openai_client_lock():
            client = getattr(self, "client", None)
            if client is not None and not self._is_openai_client_closed(client):
                return client
            try:
                new_client = self._create_openai_client(
                    self._client_kwargs, reason=reason, shared=True
                )
            except Exception as exc:
                logger.warning(
                    "Failed to recreate closed OpenAI client (%s) %s error=%s",
                    reason,
                    self._client_log_context(),
                    exc,
                )
                raise RuntimeError("Failed to recreate closed OpenAI client") from exc
            self.client = new_client
        logger.warning(
            "Detected closed shared OpenAI client; recreated before use (%s) %s",
            reason,
            self._client_log_context(),
        )
        self._close_openai_client(client, reason=f"replace:{reason}", shared=True)
        return new_client
