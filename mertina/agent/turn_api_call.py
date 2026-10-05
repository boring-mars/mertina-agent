# Ported from hermes-agent agent/turn_api_call.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""The provider call for the conversation turn's retry loop: ``perform_api_call``. Nothing here
imports ``agent.conversation_loop`` at module level (cycle).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from mertina.agent.message_metadata import append_message

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class ApiCallVerdict:
    """``action``: ``"fallthrough"`` (``response`` is ready for verification)."""

    action: str
    response: Any


def _should_stream(agent: Any) -> bool:
    """Streaming is preferred even without consumers; disabled for Mock clients in
    tests (SimpleNamespace, not stream iterators)."""
    if not agent._has_stream_consumers():
        from unittest.mock import Mock

        if isinstance(getattr(agent, "client", None), Mock):
            return False
    return True


def perform_api_call(
    agent: Any,
    *,
    api_kwargs: Any,
) -> ApiCallVerdict:
    """Issue the request (see ``_should_stream`` for the streaming decision)."""
    response = None

    def _verdict(action: str) -> ApiCallVerdict:
        return ApiCallVerdict(
            action=action,
            response=response,
        )

    _use_streaming = _should_stream(agent)

    def _perform_api_call(next_api_kwargs):
        if _use_streaming:
            return agent._interruptible_streaming_api_call(next_api_kwargs)
        return agent._interruptible_api_call(next_api_kwargs)

    response = _perform_api_call(api_kwargs)
    return _verdict("fallthrough")


@dataclass
class ApiInterruptVerdict:
    """Always ``action == "break"`` (leave the retry loop): the turn is ``interrupted`` with
    ``final_response`` set."""

    action: str
    interrupted: Any
    final_response: Any


def handle_api_interrupt(
    agent: Any,
    *,
    messages: Any,
    api_start_time: Any,
    interrupted: Any,
    final_response: Any,
) -> ApiInterruptVerdict:
    """``InterruptedError`` during the provider call: keep any streamed partial text so the
    next turn has a record of the half-finished reply."""
    from mertina.agent.conversation_loop import INTERRUPT_WAITING_FOR_MODEL_PREFIX

    api_elapsed = time.time() - api_start_time
    agent._vprint(f"{agent.log_prefix}⚡ Interrupted during API call.", force=True)
    interrupted = True
    _partial = agent._strip_think_blocks(
        getattr(agent, "_current_streamed_assistant_text", "") or ""
    ).strip()
    if _partial:
        append_message(messages, {"role": "assistant", "content": _partial})
        final_response = _partial
    else:
        final_response = f"{INTERRUPT_WAITING_FOR_MODEL_PREFIX}{api_elapsed:.1f}s elapsed)."
    return ApiInterruptVerdict("break", interrupted, final_response)
