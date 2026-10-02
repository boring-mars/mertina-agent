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

from mertina.agent.agent_runtime_helpers import _INTERRUPTED_PLACEHOLDER
from mertina.agent.message_metadata import append_message
from mertina.agent.repetition_guard import REPETITION_LOOP_INTERRUPTED, is_runaway_repetition

logger = logging.getLogger("mertina.agent.conversation_loop")


def stop_thinking_spinner(agent: Any, thinking_spinner: Any) -> None:
    """Stop the spinner silently and clear the thinking callback; returns ``None`` so
    callers can rebind ``thinking_spinner = stop_thinking_spinner(agent, thinking_spinner)``."""
    if thinking_spinner:
        thinking_spinner.stop("")
    if agent.thinking_callback:
        agent.thinking_callback("")
    return None


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
    """Always ``action == "break"`` (leave the retry loop): either a redirect restart was
    armed on ``_retry`` or the turn is ``interrupted`` with ``final_response`` set."""

    action: str
    thinking_spinner: Any
    interrupted: Any
    final_response: Any


def handle_api_interrupt(
    agent: Any,
    *,
    _retry: Any,
    thinking_spinner: Any,
    messages: Any,
    conversation_history: Any,
    api_start_time: Any,
    interrupted: Any,
    final_response: Any,
) -> ApiInterruptVerdict:
    """``InterruptedError`` during the provider call: a pending redirect keeps its correction
    queued for the outer-loop rebuild; otherwise keep any streamed partial text so the next
    turn has a record of the half-finished reply."""
    from mertina.agent.conversation_loop import INTERRUPT_WAITING_FOR_MODEL_PREFIX

    thinking_spinner = stop_thinking_spinner(agent, thinking_spinner)
    # redirect() cancelled only this request: keep the correction queued, clear the
    # cancellation bit, let the outer loop rebuild. Never materialize incomplete
    # signed/encrypted reasoning items.
    if agent._has_pending_redirect() and agent.clear_interrupt(preserve_redirect=True):
        _retry.restart_with_redirected_messages = True
        return ApiInterruptVerdict("break", thinking_spinner, interrupted, final_response)
    api_elapsed = time.time() - api_start_time
    agent._vprint(f"{agent.log_prefix}⚡ Interrupted during API call.", force=True)
    interrupted = True
    _partial = agent._strip_think_blocks(
        getattr(agent, "_current_streamed_assistant_text", "") or ""
    ).strip()
    if _partial and is_runaway_repetition(_partial):
        # The interrupted row is replayed next turn; looped bytes there re-seed the loop
        # (#112764). Same hidden shape as the redirect placeholder: nothing visible in the
        # transcript, a neutral api_content so the pre-call sanitizer does not re-heal it.
        append_message(
            messages,
            {
                "role": "assistant",
                "content": "",
                "display_kind": "hidden",
                "api_content": _INTERRUPTED_PLACEHOLDER,
            },
        )
        final_response = REPETITION_LOOP_INTERRUPTED
    elif _partial:
        append_message(messages, {"role": "assistant", "content": _partial})
        final_response = _partial
    else:
        final_response = f"{INTERRUPT_WAITING_FOR_MODEL_PREFIX}{api_elapsed:.1f}s elapsed)."
    agent._persist_session(messages, conversation_history)
    return ApiInterruptVerdict("break", thinking_spinner, interrupted, final_response)
