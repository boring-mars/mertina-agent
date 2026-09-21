"""Tool executor: sequential and concurrent dispatch into tool-result messages."""

import concurrent.futures
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from mertina import model_tools
from mertina.agent import tool_executor
from mertina.agent.agent_runtime_helpers import invoke_tool
from mertina.tools.registry import ToolRegistry


class _Agent:
    """The AIAgent surface the executor reads."""

    def __init__(self) -> None:
        self._interrupt_requested = False
        self.interrupt_reasons: list[str] = []
        self.printed: list[str] = []
        self.log_prefix = ""
        self.verbose_logging = False
        self.session_id = "sess"

    def interrupt(self, reason: str) -> None:
        self.interrupt_reasons.append(reason)
        self._interrupt_requested = True

    def _vprint(self, text: str, force: bool = False) -> None:
        self.printed.append(text)

    def _invoke_tool(self, *args: Any, **kwargs: Any) -> str:
        return invoke_tool(self, *args, **kwargs)

    def _tool_result_content_for_active_model(self, name: str, content: Any) -> Any:
        return content


def _call(call_id: str, name: str, arguments: Any = "{}") -> SimpleNamespace:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _message(*calls: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(tool_calls=list(calls))


def _schema(name: str) -> dict[str, Any]:
    return {"name": name, "description": "", "parameters": {"type": "object", "properties": {}}}


@pytest.fixture
def reg(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    fresh = ToolRegistry()
    monkeypatch.setattr(model_tools, "registry", fresh)
    return fresh


def _echo(args: dict[str, Any], **kwargs: Any) -> str:
    return json.dumps({"args": args, "task_id": kwargs.get("task_id")})


EXECUTORS = [
    tool_executor.execute_tool_calls_sequential,
    tool_executor.execute_tool_calls_concurrent,
]


# --- both executors -------------------------------------------------------------------------------


@pytest.mark.parametrize("execute", EXECUTORS)
def test_each_call_becomes_a_tool_message(reg: ToolRegistry, execute: Any) -> None:
    reg.register("echo", "demo", _schema("echo"), _echo)
    messages: list[dict[str, Any]] = []

    execute(_Agent(), _message(_call("c1", "echo", '{"x": 1}')), messages, "task")

    (message,) = messages
    assert message["role"] == "tool"
    assert message["tool_call_id"] == "c1"
    assert message["name"] == message["tool_name"] == "echo"
    assert json.loads(message["content"]) == {"args": {"x": 1}, "task_id": "task"}


@pytest.mark.parametrize("execute", EXECUTORS)
def test_invalid_arguments_are_reported_without_running_the_tool(
    reg: ToolRegistry, execute: Any
) -> None:
    ran: list[dict[str, Any]] = []

    def handler(args: dict[str, Any], **kwargs: Any) -> str:
        ran.append(args)
        return "ok"

    reg.register("t", "demo", _schema("t"), handler)
    messages: list[dict[str, Any]] = []

    execute(_Agent(), _message(_call("bad", "t", "not json"), _call("good", "t")), messages, "task")

    assert [m["tool_call_id"] for m in messages] == ["bad", "good"]
    assert json.loads(messages[0]["content"])["error"] == "Invalid tool arguments"
    assert messages[1]["content"] == "ok"
    assert ran == [{}]


@pytest.mark.parametrize("execute", EXECUTORS)
def test_an_interrupt_before_the_batch_skips_every_call(reg: ToolRegistry, execute: Any) -> None:
    reg.register("t", "demo", _schema("t"), lambda args, **kw: "ran")
    agent = _Agent()
    agent._interrupt_requested = True
    messages: list[dict[str, Any]] = []

    execute(agent, _message(_call("a", "t"), _call("b", "t")), messages, "task")

    assert [m["tool_call_id"] for m in messages] == ["a", "b"]
    assert all("skipped due to user interrupt" in m["content"] for m in messages)
    assert all(m["effect_disposition"] == "none" for m in messages)


@pytest.mark.parametrize("execute", EXECUTORS)
def test_an_unknown_tool_yields_an_error_result(reg: ToolRegistry, execute: Any) -> None:
    messages: list[dict[str, Any]] = []

    execute(_Agent(), _message(_call("c1", "nope")), messages, "task")

    assert json.loads(messages[0]["content"]) == {"error": "Unknown tool: nope"}


# --- sequential -----------------------------------------------------------------------------------


def test_sequential_runs_calls_in_order_on_the_calling_thread(reg: ToolRegistry) -> None:
    seen: list[tuple[str, int | None]] = []

    def handler(args: dict[str, Any], **kwargs: Any) -> str:
        seen.append((args["n"], threading.get_ident()))
        return "ok"

    reg.register("t", "demo", _schema("t"), handler)

    tool_executor.execute_tool_calls_sequential(
        _Agent(), _message(_call("a", "t", '{"n": "1"}'), _call("b", "t", '{"n": "2"}')), [], "task"
    )

    assert seen == [("1", threading.get_ident()), ("2", threading.get_ident())]


def test_sequential_skips_the_rest_after_an_interrupt_mid_batch(reg: ToolRegistry) -> None:
    agent = _Agent()

    def stop(args: dict[str, Any], **kwargs: Any) -> str:
        agent._interrupt_requested = True
        return "stopped"

    reg.register("stop", "demo", _schema("stop"), stop)
    reg.register("t", "demo", _schema("t"), lambda args, **kw: "ran")
    messages: list[dict[str, Any]] = []

    tool_executor.execute_tool_calls_sequential(
        agent, _message(_call("a", "stop"), _call("b", "t"), _call("c", "t")), messages, "task"
    )

    assert [m["content"] for m in messages[:1]] == ["stopped"]
    assert [m["tool_call_id"] for m in messages[1:]] == ["b", "c"]
    assert all("was not started" in m["content"] for m in messages[1:])
    assert agent.printed


def test_sequential_turns_a_dispatch_failure_into_an_error_result(
    reg: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(model_tools, "handle_function_call", fail)
    messages: list[dict[str, Any]] = []

    tool_executor.execute_tool_calls_sequential(
        _Agent(), _message(_call("c1", "t")), messages, "task"
    )

    assert messages[0]["content"] == "Error executing tool 't': boom"


def test_sequential_keyboard_interrupt_fills_the_remaining_results_and_reraises(
    reg: ToolRegistry,
) -> None:
    def ctrl_c(args: dict[str, Any], **kwargs: Any) -> str:
        raise KeyboardInterrupt

    reg.register("t", "demo", _schema("t"), ctrl_c)
    reg.register("u", "demo", _schema("u"), lambda args, **kw: "ran")
    agent = _Agent()
    messages: list[dict[str, Any]] = []

    with pytest.raises(KeyboardInterrupt):
        tool_executor.execute_tool_calls_sequential(
            agent, _message(_call("a", "t"), _call("b", "u")), messages, "task"
        )

    assert [m["tool_call_id"] for m in messages] == ["a", "b"]
    assert all("keyboard interrupt" in m["content"] for m in messages)
    assert agent.interrupt_reasons == ["keyboard interrupt"]


# --- concurrent -----------------------------------------------------------------------------------


def test_concurrent_keeps_call_order_whatever_the_finish_order(reg: ToolRegistry) -> None:
    def slow(args: dict[str, Any], **kwargs: Any) -> str:
        time.sleep(0.2)
        return "slow"

    reg.register("slow", "demo", _schema("slow"), slow)
    reg.register("fast", "demo", _schema("fast"), lambda args, **kw: "fast")
    messages: list[dict[str, Any]] = []

    tool_executor.execute_tool_calls_concurrent(
        _Agent(), _message(_call("a", "slow"), _call("b", "fast")), messages, "task"
    )

    assert [(m["tool_call_id"], m["content"]) for m in messages] == [("a", "slow"), ("b", "fast")]


def test_concurrent_runs_calls_in_parallel(reg: ToolRegistry) -> None:
    barrier = threading.Barrier(3, timeout=5)

    def meet(args: dict[str, Any], **kwargs: Any) -> str:
        barrier.wait()
        return "met"

    reg.register("meet", "demo", _schema("meet"), meet)
    calls = [_call(f"c{i}", "meet") for i in range(3)]
    messages: list[dict[str, Any]] = []

    tool_executor.execute_tool_calls_concurrent(_Agent(), _message(*calls), messages, "task")

    assert [m["content"] for m in messages] == ["met", "met", "met"]


def test_concurrent_interrupt_while_waiting_cancels_the_unfinished_calls(
    reg: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _Agent()
    release = threading.Event()

    def block(args: dict[str, Any], **kwargs: Any) -> str:
        agent._interrupt_requested = True
        release.wait(timeout=10)
        return "late"

    reg.register("block", "demo", _schema("block"), block)
    real_wait = concurrent.futures.wait

    def short_wait(fs: Any, timeout: float | None = None) -> Any:
        return real_wait(fs, timeout=min(timeout or 0.1, 0.1))

    monkeypatch.setattr(concurrent.futures, "wait", short_wait)
    messages: list[dict[str, Any]] = []
    try:
        tool_executor.execute_tool_calls_concurrent(
            agent, _message(_call("a", "block")), messages, "task"
        )
    finally:
        release.set()

    assert messages[0]["tool_call_id"] == "a"
    assert "skipped due to user interrupt" in messages[0]["content"]
    assert agent.printed


# --- invoke_tool ----------------------------------------------------------------------------------


def test_invoke_tool_dispatches_through_handle_function_call(reg: ToolRegistry) -> None:
    reg.register("echo", "demo", _schema("echo"), _echo)

    result = invoke_tool(_Agent(), "echo", {"x": 2}, "task", "c1")

    assert json.loads(result) == {"args": {"x": 2}, "task_id": "task"}


def test_invoke_tool_replaces_non_object_arguments(reg: ToolRegistry) -> None:
    reg.register("echo", "demo", _schema("echo"), _echo)

    result = invoke_tool(_Agent(), "echo", "junk", "task")  # type: ignore[arg-type]

    assert json.loads(result)["args"] == {}
