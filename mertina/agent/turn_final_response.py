# Ported from hermes-agent agent/turn_final_response.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""No-tool-call (final text) branch of the conversation turn loop: append the final answer.
Extracted from ``run_conversation``; nothing here imports ``agent.conversation_loop`` at module
level (cycle).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mertina.agent.message_metadata import append_message

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class FinalResponseVerdict:
    """``action``: ``"break"`` (turn ends with ``final_response``). The other fields are the
    loop locals rebound."""

    action: str
    final_response: Any
    _turn_exit_reason: Any


def finish_text_response(
    agent: Any,
    *,
    assistant_message: Any,
    finish_reason: Any,
    messages: Any,
    api_call_count: Any,
) -> FinalResponseVerdict:
    """Finish a text-only assistant response."""

    def _verdict(action: str) -> FinalResponseVerdict:
        return FinalResponseVerdict(
            action=action,
            final_response=final_response,
            _turn_exit_reason=_turn_exit_reason,
        )

    final_response = assistant_message.content or ""

    final_response = agent._strip_think_blocks(final_response).strip()

    final_msg = agent._build_assistant_message(assistant_message, finish_reason)

    append_message(messages, final_msg)

    _turn_exit_reason = f"text_response(finish_reason={finish_reason})"
    if not agent.quiet_mode:
        agent._safe_print(
            f"🎉 Conversation completed after {api_call_count} OpenAI-compatible API call(s)"
        )
    return _verdict("break")
