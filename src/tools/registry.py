"""Small Hermes tool registry for the v0.1 agent."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Callable, Optional

_MAX_TOOL_ERROR_CHARS = 2048
CHECK_FN_CACHE_BYPASS = ""


def tool_error(message, **extra) -> str:
    text = str(message)
    if len(text) > _MAX_TOOL_ERROR_CHARS:
        text = text[:_MAX_TOOL_ERROR_CHARS] + "… [truncated]"
    return json.dumps({"error": text, **extra}, ensure_ascii=False)


def check_fn_cache_scope() -> None:
    return None


@dataclass
class ToolEntry:
    name: str
    toolset: str
    schema: dict
    handler: Callable
    check_fn: Optional[Callable] = None
    emoji: str = ""
    max_result_size_chars: int | float | None = None
    is_async: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._generation = 0

    @staticmethod
    def current_scope_key() -> str:
        return "v0.1"

    def register(
        self, name: str, toolset: str, schema: dict, handler: Callable,
        check_fn: Callable = None, emoji: str = "",
        max_result_size_chars: int | float | None = None,
        is_async: bool = False, **_kwargs,
    ) -> None:
        if not isinstance(schema, dict):
            raise ValueError(f"Tool {name!r}: schema must be a dict")
        self._tools[name] = ToolEntry(
            name=name, toolset=toolset, schema=schema, handler=handler,
            check_fn=check_fn, emoji=emoji,
            max_result_size_chars=max_result_size_chars, is_async=is_async,
        )
        self._generation += 1

    def get_definitions(self, tool_names: set[str], quiet: bool = False) -> list[dict]:
        definitions = []
        for name in sorted(tool_names):
            entry = self._tools.get(name)
            if entry is None:
                continue
            try:
                available = entry.check_fn is None or bool(entry.check_fn())
            except Exception:
                available = False
            if available:
                definitions.append({
                    "type": "function",
                    "function": {**entry.schema, "name": entry.name},
                })
        return definitions

    def dispatch(self, name: str, args: dict, **kwargs):
        entry = self._tools.get(name)
        if entry is None:
            return tool_error(f"Unknown tool: {name}")
        try:
            result = entry.handler(args, **kwargs)
            if entry.is_async:
                result = asyncio.run(result)
            if isinstance(result, str):
                return result
            return tool_error(
                f"Tool handler returned unsupported result type: {type(result).__name__}",
                error_type="tool_result_contract",
            )
        except Exception as exc:
            return tool_error(f"Tool execution failed: {type(exc).__name__}: {exc}")

    def get_schema(self, name: str) -> Optional[dict]:
        entry = self._tools.get(name)
        return entry.schema if entry else None

    def get_toolset_for_tool(self, name: str) -> Optional[str]:
        entry = self._tools.get(name)
        return entry.toolset if entry else None

    def get_tool_names_for_toolset(self, toolset: str) -> list[str]:
        return sorted(e.name for e in self._tools.values() if e.toolset == toolset)

    def get_toolset_alias_target(self, _alias: str) -> None:
        return None

    def get_emoji(self, name: str, default: str = "") -> str:
        entry = self._tools.get(name)
        return entry.emoji if entry and entry.emoji else default

    def get_max_result_size(self, name: str, default=None):
        entry = self._tools.get(name)
        return entry.max_result_size_chars if entry and entry.max_result_size_chars is not None else default


registry = ToolRegistry()


def discover_builtin_tools(*_args, **_kwargs) -> list[str]:
    from tools import web_tools  # noqa: F401
    return ["tools.web_tools"]
