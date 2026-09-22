# Ported from hermes-agent agent/turn_tool_round.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""One tool-calling round of the conversation turn loop: append the model's tool-call turn,
then execute the tools. Nothing here imports ``agent.conversation_loop`` at module level
(cycle).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mertina.agent.message_metadata import append_message

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class ToolRoundVerdict:
    """``action``: ``"continue"`` (tools ran, next API call)."""

    action: str


def run_tool_round(
    agent: Any,
    *,
    assistant_message: Any,
    finish_reason: Any,
    messages: Any,
    api_call_count: Any,
    effective_task_id: Any,
) -> ToolRoundVerdict:
    """Execute one tool round."""

    def _verdict(action: str) -> ToolRoundVerdict:
        return ToolRoundVerdict(
            action=action,
        )

    if not agent.quiet_mode:
        agent._vprint(
            f"{agent.log_prefix}🔧 Processing {len(assistant_message.tool_calls)} tool call(s)..."
        )

    if agent.verbose_logging:
        for tc in assistant_message.tool_calls:
            raw_args = tc.function.arguments
            args_preview = raw_args[:200] if isinstance(raw_args, str) else repr(raw_args)[:200]
            logging.debug("Tool call: %s with args: %s...", tc.function.name, args_preview)

    assistant_msg = stage_tool_call_message(
        agent, assistant_message=assistant_message, finish_reason=finish_reason, messages=messages
    )
    append_message(messages, assistant_msg)

    agent._execute_tool_calls(assistant_message, messages, effective_task_id, api_call_count)

    return _verdict("continue")


def stage_tool_call_message(
    agent: Any, *, assistant_message: Any, finish_reason: Any, messages: Any
) -> dict[str, Any]:
    """Build the assistant tool-call row."""
    assistant_msg: dict[str, Any] = agent._build_assistant_message(assistant_message, finish_reason)

    return assistant_msg
