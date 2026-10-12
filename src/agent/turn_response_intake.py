"""Normalize a Chat Completions response for the v0.1 turn loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from agent.turn_truncation import normalize_response_for_agent


@dataclass
class ResponseIntakeVerdict:
    action: str
    assistant_message: Any
    finish_reason: Any
    result: Optional[Dict[str, Any]] = None
    active_system_prompt: Any = None


def _coerce_content_text(raw: Any) -> str:
    """Normalize non-string content returned by OpenAI-compatible servers."""
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
    agent: Any, *, response: Any, messages: Any, api_messages: Any,
    conversation_history: Any, api_call_count: Any, api_duration: Any,
    api_start_time: Any, api_request_id: Any, effective_task_id: Any,
    turn_id: Any, active_system_prompt: Any = None,
) -> ResponseIntakeVerdict:
    del messages, api_messages, conversation_history, api_call_count
    del api_duration, api_start_time, api_request_id, effective_task_id, turn_id
    assistant_message = normalize_response_for_agent(agent, response)
    if assistant_message.content is not None and not isinstance(
        assistant_message.content, str
    ):
        assistant_message.content = _coerce_content_text(assistant_message.content)
    content = assistant_message.content
    if content and not agent.quiet_mode:
        preview = content if agent.verbose_logging else (
            content[:100] + ("..." if len(content) > 100 else "")
        )
        agent._vprint(f"{agent.log_prefix}🤖 Assistant: {preview}")
    return ResponseIntakeVerdict(
        action="fallthrough",
        assistant_message=assistant_message,
        finish_reason=assistant_message.finish_reason,
        active_system_prompt=active_system_prompt,
    )
