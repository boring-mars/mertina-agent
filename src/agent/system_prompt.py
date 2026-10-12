"""Mertina v0.1 system prompt layers: identity, tool guidance, and time."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from agent.prompt_builder import DEFAULT_AGENT_IDENTITY, PARALLEL_TOOL_CALL_GUIDANCE


def _join_tier(parts) -> str:
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _tool_guidance(agent: Any) -> str:
    names = sorted(getattr(agent, "valid_tool_names", set()) or ())
    if not names:
        return ""
    return _join_tier([
        PARALLEL_TOOL_CALL_GUIDANCE,
        "Use the available tools when they are needed to answer accurately. "
        f"Available tools: {', '.join(names)}.",
    ])


def _timestamp_line(agent: Any) -> str:
    now = datetime.now().astimezone()
    return f"Current time: {now.isoformat(timespec='seconds')}"


def build_system_prompt_parts(
    agent: Any, system_message: Optional[str] = None,
) -> Dict[str, str]:
    return {
        "stable": _join_tier([DEFAULT_AGENT_IDENTITY, _tool_guidance(agent)]),
        "context": _join_tier([system_message]),
        "volatile": _timestamp_line(agent),
    }


def build_system_prompt(agent: Any, system_message: Optional[str] = None) -> str:
    parts = build_system_prompt_parts(agent, system_message)
    agent._cached_system_prompt_static = parts["stable"]
    return _join_tier([parts["stable"], parts["context"], parts["volatile"]])


def invalidate_system_prompt(agent: Any) -> None:
    agent._cached_system_prompt = None
    agent._cached_system_prompt_static = None


def reconstruct_static_prefix(
    agent: Any, system_message: Optional[str] = None, *, log_label: str = "restore",
) -> None:
    del log_label
    agent._cached_system_prompt_static = build_system_prompt_parts(
        agent, system_message)["stable"]


def restore_plugin_prompt_sections(agent: Any, prompt: str) -> None:
    del prompt
    agent._plugin_system_prompt_sections_snapshot = ()


def platform_hint(agent: Any) -> str:
    del agent
    return ""


def format_tools_for_system_message(agent: Any) -> str:
    if not agent.tools:
        return "[]"
    return json.dumps([
        {
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "parameters": tool["function"].get("parameters", {}),
            "required": None,
        }
        for tool in agent.tools
    ], ensure_ascii=False)


__all__ = [
    "build_system_prompt_parts", "build_system_prompt", "invalidate_system_prompt",
    "reconstruct_static_prefix", "restore_plugin_prompt_sections", "platform_hint",
    "format_tools_for_system_message",
]
