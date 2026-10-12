"""Hermes tool definitions and dispatch for the v0.1 agent."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any, Optional

from tools.registry import _MAX_TOOL_ERROR_CHARS, discover_builtin_tools, registry, tool_error

discover_builtin_tools()

_last_resolved_tool_names: list[str] = []
_LEGACY_TOOL_ALIASES = {}


@contextmanager
def suppress_post_tool_call_hook():
    yield


def _emit_post_tool_call_hook(**_kwargs) -> None:
    return None


def get_tool_definitions(
    enabled_toolsets: Optional[list[str]] = None,
    disabled_toolsets: Optional[list[str]] = None,
    quiet_mode: bool = False,
    skip_tool_search_assembly: bool = False,
) -> list[dict]:
    del skip_tool_search_assembly
    enabled = set(enabled_toolsets or ["web"])
    disabled = set(disabled_toolsets or [])
    names = set()
    if "web" in enabled or "search" in enabled or "all" in enabled or "*" in enabled:
        names.add("web_search")
    if "web" in disabled or "search" in disabled or "all" in disabled or "*" in disabled:
        names.discard("web_search")
    definitions = registry.get_definitions(names, quiet=quiet_mode)
    global _last_resolved_tool_names
    _last_resolved_tool_names = [item["function"]["name"] for item in definitions]
    return definitions


_TOOL_ERROR_STRIP_RES = (
    re.compile(r"</?(?:tool_call|function_call|result|response|output|input|system|assistant|user)>", re.I),
    re.compile(r"^\s*```(?:json|xml|html|markdown)?\s*", re.M),
    re.compile(r"\s*```\s*$", re.M),
)


def _sanitize_tool_error(error_msg: str) -> str:
    sanitized = error_msg or ""
    for pattern in _TOOL_ERROR_STRIP_RES:
        sanitized = pattern.sub("", sanitized)
    if len(sanitized) > _MAX_TOOL_ERROR_CHARS:
        sanitized = sanitized[:_MAX_TOOL_ERROR_CHARS - 3] + "..."
    return f"[TOOL_ERROR] {sanitized}"


def handle_function_call(
    function_name: str,
    function_args: dict[str, Any],
    task_id: Optional[str] = None,
    **kwargs,
) -> str:
    function_name = _LEGACY_TOOL_ALIASES.get(function_name, function_name)
    if not isinstance(function_args, dict):
        function_args = {}
    try:
        return registry.dispatch(
            function_name,
            function_args,
            task_id=task_id,
            session_id=kwargs.get("session_id"),
            user_task=kwargs.get("user_task"),
        )
    except Exception as exc:
        return tool_error(_sanitize_tool_error(f"Error executing {function_name}: {exc}"))


def get_toolset_for_tool(tool_name: str) -> Optional[str]:
    return registry.get_toolset_for_tool(tool_name)
