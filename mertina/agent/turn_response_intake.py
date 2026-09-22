# Ported from hermes-agent agent/turn_response_intake.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Response intake for the conversation turn loop: normalize the raw provider response into
the assistant message. Nothing here imports ``agent.conversation_loop`` at module level (cycle).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from mertina.agent.turn_truncation import (
    normalize_response_for_agent,
)

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class ResponseIntakeVerdict:
    """``action``: ``"fallthrough"`` (process ``assistant_message``).
    ``assistant_message``/``finish_reason`` are the normalized outputs."""

    action: str
    assistant_message: Any
    finish_reason: Any


def _coerce_content_text(raw: Any) -> str:
    """Some OpenAI-compatible servers (llama-server) return content as dict/list, which
    crashes downstream ``.strip()``; normalize to str (multimodal lists → text parts)."""
    if isinstance(raw, dict):
        return raw.get("text", "") or raw.get("content", "") or json.dumps(raw)
    if isinstance(raw, list):
        parts = []
        for part in raw:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
            elif isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
        return "\n".join(parts)
    return str(raw)


def normalize_model_response(
    agent: Any,
    *,
    response: Any,
) -> ResponseIntakeVerdict:
    """Normalize ``response`` into ``assistant_message`` (str content, never dict/list)."""
    assistant_message = normalize_response_for_agent(agent, response)
    finish_reason = assistant_message.finish_reason

    def _verdict(action: str) -> ResponseIntakeVerdict:
        return ResponseIntakeVerdict(
            action=action,
            assistant_message=assistant_message,
            finish_reason=finish_reason,
        )

    if assistant_message.content is not None and not isinstance(assistant_message.content, str):
        assistant_message.content = _coerce_content_text(assistant_message.content)

    content = assistant_message.content
    if content and not agent.quiet_mode:
        if agent.verbose_logging:
            agent._vprint(f"{agent.log_prefix}🤖 Assistant: {content}")
        else:
            agent._vprint(
                f"{agent.log_prefix}🤖 Assistant: {content[:100]}{'...' if len(content) > 100 else ''}"  # noqa: E501  # upstream's message
            )

    return _verdict("fallthrough")
