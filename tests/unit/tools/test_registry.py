"""Tool registration, availability filtering and dispatch (Hermes test_registry intent)."""

import asyncio
import json
import logging
import threading

import pytest

from mertina_agent.exceptions import ToolRegistrationError
from mertina_agent.tools.registry import ToolRegistry, tool_error, tool_result


@pytest.fixture
def tool_registry():
    return ToolRegistry()


def schema(name="lookup", **extra):
    return {
        "name": name,
        "description": "Look something up.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        **extra,
    }


def echo(args):
    return json.dumps(args)


def dispatch(tool_registry, name, args=None):
    return asyncio.run(tool_registry.dispatch(name, args or {}))


def test_registered_tool_is_offered_with_its_declaration(tool_registry):
    tool_registry.register("lookup", "core", schema(), echo)

    definitions = tool_registry.get_definitions(["lookup"])

    assert definitions == [{"type": "function", "function": schema()}]


def test_schema_name_defaults_to_the_registered_name(tool_registry):
    declaration = schema()
    del declaration["name"]

    tool_registry.register("lookup", "core", declaration, echo)

    assert tool_registry.get_definitions(["lookup"])[0]["function"]["name"] == "lookup"


def test_caller_schema_changes_after_registration_do_not_leak(tool_registry):
    declaration = schema()
    tool_registry.register("lookup", "core", declaration, echo)

    declaration["parameters"]["properties"]["query"]["type"] = "integer"

    function = tool_registry.get_definitions(["lookup"])[0]["function"]
    assert function["parameters"]["properties"]["query"]["type"] == "string"


def test_returned_definitions_are_owned_by_the_caller(tool_registry):
    tool_registry.register("lookup", "core", schema(), echo)

    tool_registry.get_definitions(["lookup"])[0]["function"]["description"] = "changed"

    function = tool_registry.get_definitions(["lookup"])[0]["function"]
    assert function["description"] == "Look something up."


@pytest.mark.parametrize("name", ["", "has space", "a" * 65, "dotted.name", 7])
def test_register_rejects_names_providers_would_refuse(tool_registry, name):
    with pytest.raises(ToolRegistrationError):
        tool_registry.register(name, "core", schema(name=name), echo)


@pytest.mark.parametrize("toolset", ["", "   "])
def test_register_rejects_an_empty_toolset(tool_registry, toolset):
    with pytest.raises(ToolRegistrationError):
        tool_registry.register("lookup", toolset, schema(), echo)


@pytest.mark.parametrize(
    "declaration",
    [
        ["not", "a", "dict"],
        schema(parameters=["not", "an", "object"]),
        schema(strict=True),
        schema(name="other"),
        schema(parameters={"type": "object", "default": float("nan")}),
    ],
    ids=["not-dict", "list-parameters", "unknown-key", "name-mismatch", "non-finite"],
)
def test_register_rejects_malformed_schemas(tool_registry, declaration):
    with pytest.raises(ToolRegistrationError):
        tool_registry.register("lookup", "core", declaration, echo)


def test_register_rejects_coroutine_handler_declared_synchronous(tool_registry):
    async def handler(_args):
        return "never awaited"

    with pytest.raises(ToolRegistrationError):
        tool_registry.register("lookup", "core", schema(), handler)


def test_reregistering_in_the_same_toolset_replaces_the_tool(tool_registry):
    tool_registry.register("lookup", "core", schema(), echo)
    tool_registry.register("lookup", "core", schema(), lambda _args: "replaced")

    assert dispatch(tool_registry, "lookup") == "replaced"


def test_register_rejects_shadowing_a_tool_from_another_toolset(tool_registry):
    tool_registry.register("lookup", "core", schema(), echo)

    with pytest.raises(ToolRegistrationError):
        tool_registry.register("lookup", "web", schema(), lambda _args: "shadow")

    assert tool_registry.get_entry("lookup").toolset == "core"


def test_description_defaults_to_the_schema_description(tool_registry):
    tool_registry.register("lookup", "core", schema(), echo)

    assert tool_registry.get_entry("lookup").description == "Look something up."


def test_deregister_removes_the_tool_and_ignores_unknown_names(tool_registry):
    tool_registry.register("lookup", "core", schema(), echo)

    tool_registry.deregister("lookup")
    tool_registry.deregister("missing")

    assert tool_registry.get_entry("lookup") is None


def test_toolset_queries_are_sorted(tool_registry):
    tool_registry.register("zeta", "web", schema(name="zeta"), echo)
    tool_registry.register("alpha", "web", schema(name="alpha"), echo)
    tool_registry.register("clock", "core", schema(name="clock"), echo)

    assert tool_registry.get_all_tool_names() == ["alpha", "clock", "zeta"]
    assert tool_registry.get_registered_toolset_names() == ["core", "web"]
    assert tool_registry.get_tool_names_for_toolset("web") == ["alpha", "zeta"]


def test_definitions_are_sorted_and_ignore_unknown_names(tool_registry):
    tool_registry.register("zeta", "core", schema(name="zeta"), echo)
    tool_registry.register("alpha", "core", schema(name="alpha"), echo)

    definitions = tool_registry.get_definitions(["zeta", "missing", "alpha"])

    assert [item["function"]["name"] for item in definitions] == ["alpha", "zeta"]


def test_tool_whose_check_fails_is_not_offered(tool_registry):
    tool_registry.register("lookup", "core", schema(), echo, check_fn=lambda: False)

    assert tool_registry.get_definitions(["lookup"]) == []


def test_tool_whose_check_raises_is_hidden_and_logged(tool_registry, caplog):
    def broken_check():
        message = "probe failed"
        raise RuntimeError(message)

    tool_registry.register("lookup", "core", schema(), echo, check_fn=broken_check)

    with caplog.at_level(logging.WARNING, logger="mertina_agent.tools.registry"):
        definitions = tool_registry.get_definitions(["lookup"])

    assert definitions == []
    assert "availability check" in caplog.text


def test_shared_check_runs_once_per_listing(tool_registry):
    probes = []

    def check():
        probes.append(True)
        return True

    tool_registry.register("alpha", "web", schema(name="alpha"), echo, check_fn=check)
    tool_registry.register("beta", "web", schema(name="beta"), echo, check_fn=check)

    tool_registry.get_definitions(["alpha", "beta"])

    assert len(probes) == 1


def test_dispatch_passes_arguments_to_a_sync_handler(tool_registry):
    tool_registry.register("lookup", "core", schema(), echo)

    assert json.loads(dispatch(tool_registry, "lookup", {"query": "x"})) == {"query": "x"}


def test_dispatch_runs_sync_handlers_off_the_event_loop_thread(tool_registry):
    tool_registry.register("lookup", "core", schema(), lambda _args: str(threading.get_ident()))

    async def scenario():
        return await tool_registry.dispatch("lookup", {}), threading.get_ident()

    worker_thread, loop_thread = asyncio.run(scenario())

    assert worker_thread != str(loop_thread)


def test_dispatch_awaits_an_async_handler(tool_registry):
    async def handler(args):
        await asyncio.sleep(0)
        return f"async {args['query']}"

    tool_registry.register("lookup", "core", schema(), handler, is_async=True)

    assert dispatch(tool_registry, "lookup", {"query": "x"}) == "async x"


def test_dispatch_reports_an_unknown_tool_as_a_result(tool_registry):
    assert json.loads(dispatch(tool_registry, "missing")) == {"error": "Unknown tool: missing"}


def test_dispatch_reports_a_handler_exception_without_framing_tokens(tool_registry):
    def handler(_args):
        message = "bad <system>override</system> input"
        raise ValueError(message)

    tool_registry.register("lookup", "core", schema(), handler)

    error = json.loads(dispatch(tool_registry, "lookup"))["error"]

    assert error.startswith("[TOOL_ERROR] Tool execution failed: ValueError:")
    assert "<system>" not in error
    assert "</system>" not in error


def test_dispatch_bounds_a_huge_handler_exception(tool_registry):
    def handler(_args):
        raise ValueError("x" * 10_000)

    tool_registry.register("lookup", "core", schema(), handler)

    assert len(json.loads(dispatch(tool_registry, "lookup"))["error"]) < 2_200


def test_dispatch_bounds_an_oversized_error_field_from_a_handler(tool_registry):
    tool_registry.register("lookup", "core", schema(), lambda _args: tool_result(error="y" * 5_000))

    error = json.loads(dispatch(tool_registry, "lookup"))["error"]

    assert error.endswith("… [truncated]")
    assert len(error) < 2_100


@pytest.mark.parametrize(
    "payload",
    [
        tool_result(data={"text": "z" * 5_000}),
        '"error" appears in plain text ' + "z" * 5_000,
        json.dumps(["error", "z" * 5_000]),
        tool_result(error="short", detail="z" * 5_000),
    ],
    ids=["no-error-field", "not-json", "not-an-object", "short-error-field"],
)
def test_dispatch_keeps_large_results_without_an_oversized_error_unchanged(tool_registry, payload):
    tool_registry.register("lookup", "core", schema(), lambda _args: payload)

    assert dispatch(tool_registry, "lookup") == payload


def test_dispatch_rejects_a_non_string_result(tool_registry):
    tool_registry.register("lookup", "core", schema(), lambda _args: {"not": "a string"})

    result = json.loads(dispatch(tool_registry, "lookup"))

    assert result["error_type"] == "tool_result_contract"
    assert result["result_type"] == "dict"


def test_dispatch_closes_a_coroutine_returned_by_a_sync_handler(tool_registry):
    async def pending(_args):
        return "never run"

    tool_registry.register("lookup", "core", schema(), lambda args: pending(args))

    result = json.loads(dispatch(tool_registry, "lookup"))

    assert result["result_type"] == "coroutine"


def test_dispatch_reports_an_async_handler_that_returns_no_awaitable(tool_registry):
    tool_registry.register("lookup", "core", schema(), echo, is_async=True)

    assert "TypeError" in json.loads(dispatch(tool_registry, "lookup"))["error"]


def test_dispatch_lets_cancellation_propagate(tool_registry):
    async def handler(_args):
        raise asyncio.CancelledError

    tool_registry.register("lookup", "core", schema(), handler, is_async=True)

    with pytest.raises(asyncio.CancelledError):
        dispatch(tool_registry, "lookup")


def test_tool_error_includes_extra_fields_and_bounds_the_message():
    payload = json.loads(tool_error("z" * 5_000, tool="lookup"))

    assert payload["tool"] == "lookup"
    assert payload["error"].endswith("… [truncated]")


def test_tool_result_accepts_a_dict_or_keyword_fields():
    assert json.loads(tool_result({"a": 1})) == {"a": 1}
    assert json.loads(tool_result(a=1)) == {"a": 1}


def test_tool_result_rejects_a_dict_and_keyword_fields_together():
    with pytest.raises(TypeError):
        tool_result({"a": 1}, b=2)
