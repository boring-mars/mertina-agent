"""Agent-level ("inline") tool executors shared by the sequential and concurrent tool paths.

These tools need live ``AIAgent`` state (stores, callbacks, session DB) and therefore
bypass the tool registry. Each executor is ``fn(agent, args, ctx) -> result``; the
table replaces two hand-maintained if/elif chains (``invoke_tool`` and
``execute_tool_calls_sequential``) that had drifted apart. Tool modules are imported
lazily at call time so ``patch("tools.x.y")`` in tests keeps working.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable, Dict, Optional, Tuple


def tool_hook_ids(agent, effective_task_id: str, tool_call_id: Optional[str]) -> Dict[str, str]:
    """Identity kwargs every tool hook/middleware call carries (all coerced to ``""``)."""
    return {
        "task_id": effective_task_id or "",
        "session_id": getattr(agent, "session_id", "") or "",
        "tool_call_id": tool_call_id or "",
        "turn_id": getattr(agent, "_current_turn_id", "") or "",
        "api_request_id": getattr(agent, "_current_api_request_id", "") or "",
    }


def emit_terminal_post_tool_call(
    agent,
    *,
    function_name: str,
    function_args: dict,
    result: Any,
    effective_task_id: str,
    tool_call_id: Optional[str],
    duration_ms: int = 0,
    status: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    middleware_trace: Optional[list] = None,
) -> None:
    """Emit the one terminal ``post_tool_call`` hook for a tool_call_id (best-effort)."""
    try:
        from model_tools import _emit_post_tool_call_hook
        _emit_post_tool_call_hook(
            function_name=function_name,
            function_args=function_args,
            result=result,
            **tool_hook_ids(agent, effective_task_id, tool_call_id),
            duration_ms=duration_ms,
            status=status,
            error_type=error_type,
            error_message=error_message,
            middleware_trace=list(middleware_trace or []),
        )
    except Exception:
        pass


@dataclass
class InlineToolContext:
    """Per-call state an inline executor may need beyond its arguments."""

    effective_task_id: str
    tool_call_id: Optional[str] = None
    messages: Optional[list] = None


InlineToolExecutor = Callable[[Any, dict, InlineToolContext], Any]



























INLINE_TOOL_EXECUTORS: Dict[str, InlineToolExecutor] = {
}




