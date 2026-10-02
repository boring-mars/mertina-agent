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


def perform_api_call(
    agent: Any,
    *,
    api_kwargs: Any,
) -> ApiCallVerdict:
    """Issue the request."""
    response = None

    def _verdict(action: str) -> ApiCallVerdict:
        return ApiCallVerdict(
            action=action,
            response=response,
        )

    response = agent._interruptible_api_call(api_kwargs)
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
