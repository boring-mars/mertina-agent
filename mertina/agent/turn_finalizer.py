# Ported from hermes-agent agent/turn_finalizer.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Post-loop turn finalization for ``run_conversation``.

Budget summary, transcript tail, diagnostics, result assembly. Synchronous, single return.
``logger`` is imported lazily from ``agent.conversation_loop`` (no cycle, same logger name)."""

from __future__ import annotations

import logging
from typing import Any

from mertina.agent.message_metadata import append_message
from mertina.agent.message_sanitization import _sanitize_surrogates

# ``result[key] = agent.session_<key>`` for the per-session usage counters.
_SESSION_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
)


def _resolve_budget_fallback(
    agent: Any,
    *,
    final_response: Any,
    api_call_count: int,
    interrupted: bool,
    failed: bool,
    messages: list[dict[str, Any]],
    _turn_exit_reason: str,
    logger: logging.Logger,
) -> tuple[Any, Any]:
    """Iteration-budget exhaustion. Returns ``(final_response, _turn_exit_reason)``."""
    budget_exhausted = (
        api_call_count >= agent.max_iterations or agent.iteration_budget.remaining <= 0
    )
    if (
        final_response is None
        and budget_exhausted
        and not interrupted
        and not failed
        and str(_turn_exit_reason) in {"unknown", "budget_exhausted"}
    ):
        _turn_exit_reason = f"max_iterations_reached({api_call_count}/{agent.max_iterations})"
        # _handle_max_iterations makes one extra toolless request for a summary.
        if not agent.quiet_mode:
            agent._safe_print(
                f"\n⚠️  Iteration budget exhausted ({api_call_count}/{agent.max_iterations}) "
                "— requesting summary...",
                diagnostic=True,
            )
        final_response = agent._handle_max_iterations(messages, api_call_count)
    return final_response, _turn_exit_reason


def _close_transcript_tail(
    agent: Any, messages: list[dict[str, Any]], final_response: Any, interrupted: bool
) -> None:
    """Shape the transcript tail."""
    # An interrupt can leave a tool result as the tail; close the sequence so strict
    # providers don't see ``tool → user`` (placeholder: final_response is usually empty).
    if interrupted:
        from mertina.agent.message_sanitization import close_interrupted_tool_sequence

        close_interrupted_tool_sequence(messages, final_response)

    # Enforce "delivered final_response ⇒ assistant row": the budget summary returns a
    # final_response with no closing assistant row.
    if final_response and not interrupted:
        _tail = messages[-1] if messages else None
        if not isinstance(_tail, dict) or _tail.get("role") != "assistant":
            append_message(messages, {"role": "assistant", "content": final_response})


def _log_turn_exit(
    agent: Any,
    messages: list[dict[str, Any]],
    final_response: Any,
    api_call_count: int,
    _turn_exit_reason: str,
    interrupted: bool,
    logger: logging.Logger,
) -> None:
    """Always INFO so agent.log captures WHY every turn ended; WARNING when the last
    message is a tool result (the "just stops" scenario)."""
    _last_msg_role = messages[-1].get("role") if messages else None
    _last_tool_name = None
    if _last_msg_role == "tool":
        # Walk back to the assistant message with the tool call.
        for _m in reversed(messages):
            if _m.get("role") == "assistant" and _m.get("tool_calls"):
                _tcs = _m["tool_calls"]
                if _tcs and isinstance(_tcs[0], dict):
                    _last_tool_name = _tcs[-1].get("function", {}).get("name")
                break

    _turn_tool_count = sum(
        1
        for m in messages
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
    )
    _diag_msg = (
        "Turn ended: reason=%s model=%s api_calls=%d/%d budget=%d/%d "
        "tool_turns=%d last_msg_role=%s response_len=%d session=%s"
    )
    _diag_args = (
        _turn_exit_reason,
        agent.model,
        api_call_count,
        agent.max_iterations,
        agent.iteration_budget.used if agent.iteration_budget else 0,
        agent.iteration_budget.max_total if agent.iteration_budget else 0,
        _turn_tool_count,
        _last_msg_role,
        len(final_response) if final_response else 0,
        agent.session_id or "none",
    )
    if _last_msg_role == "tool" and not interrupted:
        logger.warning(
            "Turn ended with pending tool result (agent may appear stuck). "
            + _diag_msg
            + " last_tool=%s",
            *_diag_args,
            _last_tool_name,
        )
    else:
        logger.info(_diag_msg, *_diag_args)


def _last_turn_reasoning(messages: list[dict[str, Any]]) -> Any | None:
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
    agent: Any,
    *,
    final_response: Any,
    api_call_count: int,
    interrupted: bool,
    failed: bool,
    messages: list[dict[str, Any]],
    _turn_exit_reason: str,
) -> dict[str, Any]:
    """Run the post-loop finalization and return the turn ``result`` dict."""
    from mertina.agent.conversation_loop import logger

    final_response, _turn_exit_reason = _resolve_budget_fallback(
        agent,
        final_response=final_response,
        api_call_count=api_call_count,
        interrupted=interrupted,
        failed=failed,
        messages=messages,
        _turn_exit_reason=_turn_exit_reason,
        logger=logger,
    )

    completed = (
        final_response is not None
        and not failed
        and not interrupted
        and (
            api_call_count < agent.max_iterations
            or str(_turn_exit_reason).startswith("text_response(")
        )
    )

    _close_transcript_tail(agent, messages, final_response, interrupted)

    _log_turn_exit(
        agent, messages, final_response, api_call_count, _turn_exit_reason, interrupted, logger
    )

    # Surrogate chokepoint: RAW SDK text with a lone UTF-16 surrogate crashes downstream
    # consumers (stdout, Telegram ``utf16_len``, JSON); scrub once where it leaves the loop.
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
        "partial": False,  # no v0.1 exit stops a turn part-way
        "interrupted": interrupted,
        "model": agent.model,
        "provider": agent.provider,
        "base_url": agent.base_url,
        **{key: getattr(agent, f"session_{key}") for key in _SESSION_TOKEN_KEYS},
        "session_id": agent.session_id,
    }
    if interrupted and agent._interrupt_message:
        result["interrupt_message"] = agent._interrupt_message
    agent.clear_interrupt()

    return result
