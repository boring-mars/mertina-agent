# Ported from hermes-agent agent/agent_runtime_helpers.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Assorted AIAgent runtime helpers (message repair/sanitization, credential recovery, primary
runtime restore, prompt-cache policy, client construction, model switching, tool invocation).
Each function takes the parent ``AIAgent`` as ``agent`` except the stateless message helpers.
``_ra()`` resolves ``run_agent`` lazily so tests patching ``run_agent.X`` keep intercepting.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)


def _ra() -> ModuleType:
    """Lazy ``run_agent`` reference for test-patch routing."""
    from mertina import run_agent

    return run_agent


def create_openai_client(
    agent: Any, client_kwargs: dict[str, Any], *, reason: str, shared: bool
) -> Any:
    # Treat client_kwargs as read-only: callers pass agent._client_kwargs, and in-place mutation
    # leaks into later requests (a torn-down httpx transport got reused).
    # Callers pass agent._client_kwargs (or shallow copies of it) in; any in-place mutation leaks
    # back into the stored dict and is reused on subsequent requests. #10933 hit this by injecting
    # an httpx.Client transport that was torn down after the first request, so the next request
    # wrapped a closed transport and raised "Cannot send a request, as the client has been closed"
    # on every retry. The revert resolved that specific path; this copy locks the contract so future
    # transport/keepalive work can't reintroduce the same class of bug.
    client_kwargs = dict(client_kwargs)
    # Retries belong to the outer conversation loop (honors Retry-After); SDK retries would
    # double-retry inside it. auxiliary_client keeps SDK retries as it isn't wrapped.
    # Delegate all rate-limit / 5xx retry to hermes's outer conversation loop, which honors
    # Retry-After and applies adaptive/jittered backoff. The OpenAI SDK default (max_retries=2) uses
    # its own 1-2s backoff that ignores Retry-After and double-retries inside our loop — the same
    # deadlock the Anthropic clients hit (#26293). This is the single chokepoint every primary
    # OpenAI/aggregator client passes through (init, switch_model, recovery, restore,
    # request-scoped); auxiliary_client builds its own clients and keeps SDK retries because it is
    # NOT wrapped by the conversation loop.
    client_kwargs.setdefault("max_retries", 0)
    # ``process_bootstrap.OpenAI`` is a lazy SDK proxy; resolved at call time so tests can patch it.
    from mertina.agent import process_bootstrap

    client = process_bootstrap.OpenAI(**client_kwargs)
    _ra().logger.info(
        "OpenAI client created (%s, shared=%s) %s", reason, shared, agent._client_log_context()
    )
    return client
