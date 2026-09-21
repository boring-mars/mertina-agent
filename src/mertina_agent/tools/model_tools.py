"""Loop-facing tool API: which tools to offer the model, and how to call one.

Adapted from Hermes model_tools.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md for the pinned source and reductions.

Hermes resolves toolsets through ``toolsets.py`` and routes calls through hooks,
middleware, Tool Search and connector bridges. Mertina selects directly from
the toolsets registered in the registry and dispatches straight to it.
"""

import importlib
from collections.abc import Collection, Sequence

from mertina_agent.agent.transports.types import JsonObject, ToolDefinition
from mertina_agent.exceptions import ConfigurationError
from mertina_agent.tools.registry import ToolRegistry, registry, tool_error

# Built-in tool modules register into the default registry when imported.
# Hermes discovers them by scanning tools/; an explicit list keeps start-up
# predictable and makes the set of built-in tools reviewable in one place.
_BUILTIN_TOOL_MODULES = ("mertina_agent.tools.web_tools",)


def discover_builtin_tools() -> list[str]:
    """Import every built-in tool module; return the module names, in order."""
    for module_name in _BUILTIN_TOOL_MODULES:
        importlib.import_module(module_name)
    return list(_BUILTIN_TOOL_MODULES)


discover_builtin_tools()


def select_tool_names(
    enabled_toolsets: Sequence[str] | None = None,
    disabled_toolsets: Sequence[str] | None = None,
    *,
    tool_registry: ToolRegistry = registry,
) -> set[str]:
    """Resolve toolset selections to the set of tool names they contain.

    Args:
        enabled_toolsets: Toolsets to offer; ``None`` offers every toolset.
        disabled_toolsets: Toolsets removed after enabling.
        tool_registry: Registry to resolve against.

    Raises:
        ConfigurationError: If a named toolset has no registered tools. Hermes
            only warns here; an agent silently running without the tools it was
            configured with is harder to diagnose than a startup failure.
    """
    known = set(tool_registry.get_registered_toolset_names())
    requested = [*(enabled_toolsets or ()), *(disabled_toolsets or ())]
    unknown = sorted({toolset for toolset in requested if toolset not in known})
    if unknown:
        message = f"Unknown toolsets: {', '.join(unknown)}"
        raise ConfigurationError(message)

    enabled = known if enabled_toolsets is None else set(enabled_toolsets)
    selected = enabled - set(disabled_toolsets or ())
    return {
        name for toolset in selected for name in tool_registry.get_tool_names_for_toolset(toolset)
    }


def get_tool_definitions(
    enabled_toolsets: Sequence[str] | None = None,
    disabled_toolsets: Sequence[str] | None = None,
    *,
    tool_registry: ToolRegistry = registry,
) -> list[ToolDefinition]:
    """Return declarations for the selected tools that are currently available.

    The result is sorted by tool name and owned by the caller.

    Raises:
        ConfigurationError: If a named toolset has no registered tools.
    """
    names = select_tool_names(enabled_toolsets, disabled_toolsets, tool_registry=tool_registry)
    return tool_registry.get_definitions(names)


async def handle_function_call(
    function_name: str,
    function_args: JsonObject,
    *,
    enabled_tools: Collection[str] | None = None,
    tool_registry: ToolRegistry = registry,
) -> str:
    """Run one tool call and return its result string.

    Args:
        function_name: Tool name as emitted by the model.
        function_args: Arguments already parsed into a JSON object.
        enabled_tools: Names offered to the model this turn. A registered tool
            outside this set is refused, so a model cannot reach a tool it was
            never offered. ``None`` allows every registered tool.
        tool_registry: Registry that owns the handlers.

    Returns:
        The tool's result, or a JSON error the model can read. Tool failures
        never raise; cancellation propagates.
    """
    if enabled_tools is not None and function_name not in enabled_tools:
        return tool_error(f"Tool '{function_name}' is not enabled for this conversation")
    return await tool_registry.dispatch(function_name, function_args)
