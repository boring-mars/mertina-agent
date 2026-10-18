"""Toolset selection and the loop-facing call entry point (Hermes test_model_tools intent)."""

import asyncio
import json

import pytest

from mertina_agent.exceptions import ConfigurationError
from mertina_agent.tools.model_tools import (
    get_tool_definitions,
    handle_function_call,
    select_tool_names,
)
from mertina_agent.tools.registry import ToolRegistry


@pytest.fixture
def tool_registry():
    tool_registry = ToolRegistry()
    for name, toolset in (("search", "web"), ("fetch", "web"), ("clock", "core")):
        tool_registry.register(name, toolset, {"name": name}, lambda _args, name=name: name)
    return tool_registry


def test_no_selection_offers_every_toolset(tool_registry):
    assert select_tool_names(tool_registry=tool_registry) == {"search", "fetch", "clock"}


def test_enabled_toolsets_limit_the_selection(tool_registry):
    assert select_tool_names(["core"], tool_registry=tool_registry) == {"clock"}


def test_disabled_toolsets_are_removed_after_enabling(tool_registry):
    assert select_tool_names(None, ["web"], tool_registry=tool_registry) == {"clock"}


def test_empty_enabled_list_offers_no_tools(tool_registry):
    assert select_tool_names([], tool_registry=tool_registry) == set()


@pytest.mark.parametrize(
    ("enabled", "disabled"), [(["missing"], None), (None, ["missing"])], ids=["enabled", "disabled"]
)
def test_unknown_toolset_fails_fast(tool_registry, enabled, disabled):
    with pytest.raises(ConfigurationError, match="missing"):
        select_tool_names(enabled, disabled, tool_registry=tool_registry)


def test_definitions_cover_only_available_selected_tools(tool_registry):
    tool_registry.register("offline", "core", {"name": "offline"}, str, check_fn=lambda: False)

    definitions = get_tool_definitions(["core"], tool_registry=tool_registry)

    assert [item["function"]["name"] for item in definitions] == ["clock"]


def test_call_runs_an_enabled_tool(tool_registry):
    result = asyncio.run(
        handle_function_call("clock", {}, enabled_tools={"clock"}, tool_registry=tool_registry)
    )

    assert result == "clock"


def test_call_refuses_a_registered_tool_that_was_not_offered(tool_registry):
    result = asyncio.run(
        handle_function_call("search", {}, enabled_tools={"clock"}, tool_registry=tool_registry)
    )

    assert "not enabled" in json.loads(result)["error"]


def test_call_without_an_enabled_set_allows_every_registered_tool(tool_registry):
    assert asyncio.run(handle_function_call("search", {}, tool_registry=tool_registry)) == "search"
