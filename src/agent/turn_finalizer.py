"""Post-loop turn finalization for ``run_conversation``.

Budget summary, trajectory save, persist, diagnostics, response transforms, result
assembly, steer drain, memory/skill review. Synchronous, single return. ``logger`` is
imported lazily from ``agent.conversation_loop`` (no cycle, same logger name)."""

from __future__ import annotations

import logging
import os
from contextlib import suppress
from typing import Any, Callable, List, Optional, Tuple

from agent.codex_responses_adapter import _summarize_user_message_for_log
from agent.delegation_context import is_dispatcher_owned_worker_context
from agent.turn_failure_copy import exit_reason_failure, stamp_failure
from agent.context_compressor import _DB_PERSISTED_MARKER
from agent.message_content import flatten_message_text
from agent.message_metadata import append_message, stamp_message_timestamp
from agent.message_sanitization import _sanitize_surrogates
from agent.served_model import result_model_fields

# Verification-continuation nudges (verify-on-stop / pre_verify) must be stripped from
# returned/live history to avoid role-alternation breaks; the assistant response is
# real content and is not flagged. (#65919)
_VERIFICATION_CONTINUATION_FLAGS = ("_verification_stop_synthetic", "_pre_verify_synthetic")

_SENTENCE_END = {".", "!", "?", "。", "！", "？", "`", ")"}

# ``result[key] = agent.session_<key>`` for the per-session usage/cost counters.
_SESSION_TOKEN_KEYS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
)
_SESSION_COST_KEYS = ("estimated_cost_usd", "cost_status", "cost_source")
































def _last_turn_reasoning(messages) -> Optional[Any]:
    """Reasoning from the CURRENT turn only: stop at this turn's user message (#17055),
    but take the most recent non-empty reasoning since many providers emit it on the
    tool-call step and leave the final step with reasoning=None."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return None  # turn boundary — don't cross into prior turns
        if msg.get("role") == "assistant" and msg.get("reasoning"):
            return msg["reasoning"]
    return None



def finalize_turn(
    agent, *, final_response, api_call_count, interrupted, failed, messages, conversation_history,
    effective_task_id, turn_id, user_message, original_user_message, _should_review_memory,
    _turn_exit_reason, _pending_verification_response=None,
    _pending_verification_response_previewed=False,
):
    """Run the post-loop finalization and return the turn ``result`` dict."""
    from agent.conversation_loop import logger

    _exit_failure = None if interrupted else exit_reason_failure(_turn_exit_reason)
    if _exit_failure is not None and _exit_failure.fails_turn:
        failed = True

    completed = (
        final_response is not None
        and not failed
        and not interrupted
        and (api_call_count < agent.max_iterations or str(_turn_exit_reason).startswith("text_response("))
    )

    with suppress(Exception):
        agent._session_messages = messages

    if isinstance(final_response, str):
        final_response = _sanitize_surrogates(final_response)

    result = {
        "final_response": final_response,
        "last_reasoning": _last_turn_reasoning(messages),
        "messages": messages,
        "api_calls": api_call_count,
        "completed": completed,
        "turn_exit_reason": _turn_exit_reason,
        "failed": failed,
        "partial": False,  # True only when stopped due to invalid tool calls
        "interrupted": interrupted,
        "response_transformed": False,
        "pre_transform_response": None,
        "response_previewed": getattr(agent, "_response_was_previewed", False),
        "model": agent.model,
        **result_model_fields(agent),
        "provider": agent.provider,
        "base_url": agent.base_url,
        **{key: getattr(agent, f"session_{key}") for key in _SESSION_TOKEN_KEYS},
        **{key: getattr(agent, f"session_{key}") for key in _SESSION_COST_KEYS},
        "session_id": agent.session_id,
    }
    agent._response_was_previewed = False
    if interrupted and agent._interrupt_message:
        result["interrupt_message"] = agent._interrupt_message
    agent.clear_interrupt()
    agent._stream_callback = None  # don't leak into future calls
    agent._turn_preflight_display_snapshot = None
    agent._turn_received_provider_response = False
    return result
