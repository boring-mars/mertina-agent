"""ToolRegistry: registration, schema retrieval, dispatch and built-in tool discovery."""

import importlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from mertina.tools.registry import (
    _MAX_TOOL_ERROR_CHARS,
    ToolRegistry,
    discover_builtin_tools,
    tool_error,
    tool_result,
)


def _schema(name: str, description: str = "A tool.") -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": {}},
    }


def _echo(args: dict[str, Any], **kwargs: Any) -> str:
    return json.dumps({"args": args, "kwargs": kwargs})


@pytest.fixture
def reg() -> ToolRegistry:
    return ToolRegistry()


# --- register -------------------------------------------------------------------------------------


def test_register_stores_an_entry_and_bumps_the_generation(reg: ToolRegistry) -> None:
    reg.register("echo", "demo", _schema("echo", "Echo it."), _echo, emoji="E")

    entry = reg.get_entry("echo")
    assert entry is not None
    assert (entry.toolset, entry.description, entry.emoji) == ("demo", "Echo it.", "E")
    assert entry.requires_env == []
    assert reg._generation == 1


def test_register_rejects_a_non_dict_schema(reg: ToolRegistry) -> None:
    with pytest.raises(ValueError, match="schema must be a dict"):
        reg.register("bad", "demo", ["not", "a", "dict"], _echo)  # type: ignore[arg-type]


def test_register_rejects_non_object_parameters(reg: ToolRegistry) -> None:
    schema = {"name": "bad", "parameters": ["x"]}
    with pytest.raises(ValueError, match="must be an object"):
        reg.register("bad", "demo", schema, _echo)


def test_same_toolset_re_registration_replaces_the_entry(reg: ToolRegistry) -> None:
    reg.register("echo", "demo", _schema("echo", "old"), _echo)
    reg.register("echo", "demo", _schema("echo", "new"), _echo)

    entry = reg.get_entry("echo")
    assert entry is not None and entry.description == "new"


def test_cross_toolset_shadow_is_rejected_without_override(
    reg: ToolRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    reg.register("echo", "first", _schema("echo"), _echo)
    with caplog.at_level(logging.ERROR, logger="mertina.tools.registry"):
        reg.register("echo", "second", _schema("echo"), _echo)

    entry = reg.get_entry("echo")
    assert entry is not None and entry.toolset == "first"
    assert "REJECTED" in caplog.text


def test_cross_toolset_override_is_allowed_when_asked_for(reg: ToolRegistry) -> None:
    reg.register("echo", "first", _schema("echo"), _echo)
    reg.register("echo", "second", _schema("echo"), _echo, override=True)

    entry = reg.get_entry("echo")
    assert entry is not None and entry.toolset == "second"


# --- get_definitions ------------------------------------------------------------------------------


def test_definitions_are_openai_function_schemas_sorted_by_name(reg: ToolRegistry) -> None:
    reg.register("zeta", "demo", _schema("ignored"), _echo)
    reg.register("alpha", "demo", _schema("alpha"), _echo)

    defs = reg.get_definitions({"zeta", "alpha", "missing"})

    assert [d["type"] for d in defs] == ["function", "function"]
    assert [d["function"]["name"] for d in defs] == ["alpha", "zeta"]


def test_a_failing_or_raising_check_fn_hides_the_tool(reg: ToolRegistry) -> None:
    def boom() -> bool:
        raise RuntimeError("probe failed")

    reg.register("off", "demo", _schema("off"), _echo, check_fn=lambda: False)
    reg.register("broken", "demo", _schema("broken"), _echo, check_fn=boom)
    reg.register("on", "demo", _schema("on"), _echo, check_fn=lambda: True)

    names = [d["function"]["name"] for d in reg.get_definitions({"off", "broken", "on"})]

    assert names == ["on"]


def test_a_check_fn_shared_by_tools_is_probed_once_per_pass(reg: ToolRegistry) -> None:
    calls: list[int] = []

    def check() -> bool:
        calls.append(1)
        return True

    reg.register("a", "demo", _schema("a"), _echo, check_fn=check)
    reg.register("b", "demo", _schema("b"), _echo, check_fn=check)

    reg.get_definitions({"a", "b"})

    assert len(calls) == 1


def test_dynamic_schema_overrides_are_merged(reg: ToolRegistry) -> None:
    reg.register(
        "dyn",
        "demo",
        _schema("dyn"),
        _echo,
        dynamic_schema_overrides=lambda: {"description": "now"},
    )

    (definition,) = reg.get_definitions({"dyn"})

    assert definition["function"]["description"] == "now"


def test_raising_dynamic_schema_overrides_fall_back_to_the_static_schema(
    reg: ToolRegistry,
) -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("no config")

    reg.register("dyn", "demo", _schema("dyn", "static"), _echo, dynamic_schema_overrides=boom)

    (definition,) = reg.get_definitions({"dyn"})

    assert definition["function"]["description"] == "static"


# --- dispatch -------------------------------------------------------------------------------------


def test_dispatch_passes_args_and_kwargs_to_the_handler(reg: ToolRegistry) -> None:
    reg.register("echo", "demo", _schema("echo"), _echo)

    result = reg.dispatch("echo", {"x": 1}, task_id="t1")

    assert isinstance(result, str)
    assert json.loads(result) == {"args": {"x": 1}, "kwargs": {"task_id": "t1"}}


def test_dispatch_of_an_unknown_tool_returns_an_error(reg: ToolRegistry) -> None:
    result = reg.dispatch("nope", {})

    assert isinstance(result, str)
    assert json.loads(result) == {"error": "Unknown tool: nope"}


def test_dispatch_turns_a_handler_exception_into_a_sanitized_error(reg: ToolRegistry) -> None:
    def boom(args: dict[str, Any], **kwargs: Any) -> str:
        raise RuntimeError("bad <system>input</system>")

    reg.register("boom", "demo", _schema("boom"), boom)

    result = reg.dispatch("boom", {})

    assert isinstance(result, str)
    error = json.loads(result)["error"]
    assert error == "[TOOL_ERROR] Tool execution failed: RuntimeError: bad input"


def test_dispatch_rejects_an_unsupported_result_type(reg: ToolRegistry) -> None:
    reg.register("num", "demo", _schema("num"), lambda args, **kw: 42)

    result = reg.dispatch("num", {})

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["error_type"] == "tool_result_contract"
    assert payload["result_type"] == "int"


def test_dispatch_passes_the_multimodal_envelope_through(reg: ToolRegistry) -> None:
    envelope = {"_multimodal": True, "content": [{"type": "text", "text": "hi"}]}
    reg.register("mm", "demo", _schema("mm"), lambda args, **kw: envelope)

    assert reg.dispatch("mm", {}) is envelope


def test_dispatch_runs_an_async_handler(reg: ToolRegistry) -> None:
    async def handler(args: dict[str, Any], **kwargs: Any) -> str:
        return tool_result(ok=True)

    reg.register("async_tool", "demo", _schema("async_tool"), handler, is_async=True)

    result = reg.dispatch("async_tool", {})

    assert isinstance(result, str)
    assert json.loads(result) == {"ok": True}


def test_dispatch_bounds_an_oversized_json_error(reg: ToolRegistry) -> None:
    long_error = "x" * (_MAX_TOOL_ERROR_CHARS * 2)
    reg.register("big", "demo", _schema("big"), lambda a, **kw: json.dumps({"error": long_error}))

    result = reg.dispatch("big", {})

    assert isinstance(result, str)
    error = json.loads(result)["error"]
    assert error.endswith("… [truncated]")
    assert len(error) < len(long_error)


# --- query helpers --------------------------------------------------------------------------------


def test_query_helpers(reg: ToolRegistry) -> None:
    reg.register("b", "shown", _schema("b"), _echo)
    reg.register("a", "shown", _schema("a"), _echo)
    reg.register("c", "hidden", _schema("c"), _echo, check_fn=lambda: False)

    assert reg.get_all_tool_names() == ["a", "b", "c"]
    assert reg.get_toolset_for_tool("a") == "shown"
    assert reg.get_toolset_for_tool("missing") is None
    assert reg.check_toolset_requirements() == {"hidden": False, "shown": True}


# --- tool_error / tool_result ---------------------------------------------------------------------


def test_tool_error_adds_extra_fields_and_bounds_the_message() -> None:
    payload = json.loads(tool_error("x" * (_MAX_TOOL_ERROR_CHARS + 10), code=7))

    assert payload["code"] == 7
    assert payload["error"].endswith("… [truncated]")


def test_tool_result_takes_a_dict_or_keywords() -> None:
    assert json.loads(tool_result({"a": 1})) == {"a": 1}
    assert json.loads(tool_result(a=1)) == {"a": 1}


# --- discover_builtin_tools -----------------------------------------------------------------------


def test_discovery_imports_only_modules_that_register_at_module_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "__init__.py").write_text("")
    (tmp_path / "registry.py").write_text("registry.register('x')\n")
    (tmp_path / "direct.py").write_text("registry.register('a')\n")
    (tmp_path / "looped.py").write_text("for n in ('a', 'b'):\n    registry.register(n)\n")
    (tmp_path / "nested.py").write_text("def f():\n    registry.register('a')\n")
    (tmp_path / "plain.py").write_text("x = 1\n")
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "tool.py").write_text("registry.register('p')\n")
    imported: list[str] = []
    monkeypatch.setattr(importlib, "import_module", imported.append)

    result = discover_builtin_tools(tmp_path)

    expected = ["mertina.tools.direct", "mertina.tools.looped", "mertina.tools.pkg.tool"]
    assert imported == expected
    assert result == expected


def test_discovery_skips_a_package_without_an_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "loose"
    package.mkdir()
    (package / "tool.py").write_text("registry.register('p')\n")
    imported: list[str] = []
    monkeypatch.setattr(importlib, "import_module", imported.append)

    assert discover_builtin_tools(tmp_path) == []
    assert imported == []


def test_discovery_logs_and_skips_a_module_that_fails_to_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "broken.py").write_text("registry.register('a')\n")

    def fail(name: str) -> None:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", fail)

    with caplog.at_level(logging.WARNING, logger="mertina.tools.registry"):
        assert discover_builtin_tools(tmp_path) == []
    assert "mertina.tools.broken" in caplog.text
