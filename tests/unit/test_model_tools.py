"""model_tools: tool definitions, dispatch through handle_function_call, and the async bridge."""

import asyncio
import json
from typing import Any

import pytest

from mertina import model_tools
from mertina.tools.registry import _MAX_TOOL_ERROR_CHARS, ToolRegistry


def _schema(name: str) -> dict[str, Any]:
    return {"name": name, "description": "", "parameters": {"type": "object", "properties": {}}}


@pytest.fixture
def reg(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    """A fresh registry in place of the process-wide one."""
    fresh = ToolRegistry()
    monkeypatch.setattr(model_tools, "registry", fresh)
    return fresh


# --- get_tool_definitions -------------------------------------------------------------------------


def test_definitions_cover_every_registered_tool_that_passes_its_check(reg: ToolRegistry) -> None:
    reg.register("b", "one", _schema("b"), lambda a, **kw: "")
    reg.register("a", "two", _schema("a"), lambda a, **kw: "")
    reg.register("off", "two", _schema("off"), lambda a, **kw: "", check_fn=lambda: False)

    defs = model_tools.get_tool_definitions(quiet_mode=True)

    assert [d["function"]["name"] for d in defs] == ["a", "b"]
    assert model_tools._last_resolved_tool_names == ["a", "b"]


def test_definitions_print_the_selection_unless_quiet(
    reg: ToolRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    model_tools.get_tool_definitions()
    assert "No tools selected" in capsys.readouterr().out

    reg.register("a", "one", _schema("a"), lambda a, **kw: "")
    model_tools.get_tool_definitions()
    assert "Final tool selection (1 tools): a" in capsys.readouterr().out


# --- handle_function_call -------------------------------------------------------------------------


def test_handle_function_call_passes_the_call_context_to_the_handler(reg: ToolRegistry) -> None:
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def handler(args: dict[str, Any], **kwargs: Any) -> str:
        seen.append((args, kwargs))
        return "done"

    reg.register("t", "demo", _schema("t"), handler)

    result = model_tools.handle_function_call(
        "t", {"x": 1}, task_id="task", tool_call_id="call", session_id="sess", user_task="ask"
    )

    assert result == "done"
    assert seen == [({"x": 1}, {"task_id": "task", "session_id": "sess", "user_task": "ask"})]


def test_handle_function_call_replaces_non_object_arguments(reg: ToolRegistry) -> None:
    seen: list[dict[str, Any]] = []

    def handler(args: dict[str, Any], **kwargs: Any) -> str:
        seen.append(args)
        return "ok"

    reg.register("t", "demo", _schema("t"), handler)

    model_tools.handle_function_call("t", "not a dict")  # type: ignore[arg-type]

    assert seen == [{}]


def test_handle_function_call_reports_an_unknown_tool(reg: ToolRegistry) -> None:
    result = model_tools.handle_function_call("nope", {})

    assert json.loads(result) == {"error": "Unknown tool: nope"}


def test_handle_function_call_catches_a_dispatch_failure(
    reg: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("registry down")

    monkeypatch.setattr(reg, "dispatch", fail)

    result = model_tools.handle_function_call("t", {})

    assert json.loads(result) == {"error": "[TOOL_ERROR] Error executing t: registry down"}


# --- _sanitize_tool_error -------------------------------------------------------------------------


def test_sanitize_strips_framing_tokens() -> None:
    raw = "<tool_call>bad</tool_call> <![CDATA[x]]>\n```json\n{}\n```"

    assert model_tools._sanitize_tool_error(raw) == "[TOOL_ERROR] bad \n{}\n"


def test_sanitize_caps_the_length() -> None:
    sanitized = model_tools._sanitize_tool_error("x" * (_MAX_TOOL_ERROR_CHARS * 2))

    assert sanitized.endswith("...")
    assert len(sanitized) == len("[TOOL_ERROR] ") + _MAX_TOOL_ERROR_CHARS


def test_sanitize_of_an_empty_message() -> None:
    assert model_tools._sanitize_tool_error("") == "[TOOL_ERROR] "


# --- _run_async -----------------------------------------------------------------------------------


async def _answer() -> int:
    await asyncio.sleep(0)
    return 42


def test_run_async_from_sync_code_reuses_one_loop() -> None:
    assert model_tools._run_async(_answer()) == 42
    loop = model_tools._tool_loop
    assert model_tools._run_async(_answer()) == 42
    assert model_tools._tool_loop is loop


def test_run_async_inside_a_running_loop_uses_a_worker_thread() -> None:
    async def outer() -> Any:
        return model_tools._run_async(_answer())

    assert asyncio.run(outer()) == 42


# --- pass-throughs --------------------------------------------------------------------------------


def test_registry_pass_throughs(reg: ToolRegistry) -> None:
    reg.register("a", "one", _schema("a"), lambda a, **kw: "")

    assert model_tools.get_toolset_for_tool("a") == "one"
    assert model_tools.check_toolset_requirements() == {"one": True}
