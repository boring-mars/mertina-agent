# Ported from hermes-agent agent/turn_loop_errors.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Outer-loop exception handler for the conversation turn loop: report the error, add error
results for unanswered tool_calls and end the turn. Nothing here imports
``agent.conversation_loop`` at module level (cycle); loop-internal helpers resolve lazily.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mertina.agent.message_metadata import append_message

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class OuterErrorVerdict:
    """``action`` is ``"break"`` (turn ends with ``turn_exit_reason`` set). The other fields
    are the loop locals the handler rebinds."""

    action: str
    _turn_exit_reason: Any


def handle_outer_loop_error(
    agent: Any,
    *,
    e: Any,
    api_call_count: Any,
    messages: Any,
    _turn_exit_reason: Any,
) -> OuterErrorVerdict:
    """Handle an exception that escaped the response-processing block and end the turn. The
    assistant message is never appended here: an assistant row may already be the tail
    (assistant→assistant)."""
    from mertina.agent.conversation_loop import (
        _ra,
    )

    def _verdict(action: str) -> OuterErrorVerdict:
        return OuterErrorVerdict(
            action=action,
            _turn_exit_reason=_turn_exit_reason,
        )

    error_msg = f"Error during local message processing after API call #{api_call_count}: {e!s}"
    try:
        agent._safe_print(f"❌ {error_msg}", diagnostic=True)
    except (OSError, ValueError):
        logger.error(error_msg)

    # ERROR level with traceback so outer-loop failures land in agent.log AND errors.log.
    logger.exception("Outer loop error in API call #%d", api_call_count)

    # An appended assistant tool_calls message needs a role="tool" result per
    # tool_call_id; fill in error results for unanswered ones.
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, dict):
            break
        if msg.get("role") == "tool":
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            answered_ids = {
                m["tool_call_id"]
                for m in messages[idx + 1 :]
                if isinstance(m, dict) and m.get("role") == "tool"
            }
            for tc in msg["tool_calls"]:
                if tc and isinstance(tc, dict) and tc["id"] not in answered_ids:
                    append_message(
                        messages,
                        {
                            "role": "tool",
                            "name": _ra().AIAgent._get_tool_call_name_static(tc),
                            "tool_call_id": tc["id"],
                            "content": f"Error executing tool: {error_msg}",
                        },
                    )
        break

    # Non-tool errors are already printed; a synthetic message would pollute history
    # and risk breaking role alternation.

    _turn_exit_reason = f"local_processing_error({error_msg[:80]})"
    return _verdict("break")
