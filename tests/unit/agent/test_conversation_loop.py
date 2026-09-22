"""Conversation loop: one turn end to end against a fake model, with the real transport,
registry and tool executor."""

import json
import re
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from mertina import model_tools, run_agent
from mertina.agent import tool_executor
from mertina.agent.agent_runtime_helpers import invoke_tool
from mertina.agent.conversation_loop import run_conversation
from mertina.agent.transports.chat_completions import ChatCompletionsTransport
from mertina.tools.registry import ToolRegistry

_SESSION_KEYS = (
    "api_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
)


class _Agent:
    """The AIAgent surface the conversation loop reads, driving a scripted model."""

    session_api_calls: int  # set with the other session_* counters below

    def __init__(self, responses: list[Any], *, max_iterations: int = 10) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.summaries: list[int] = []
        self.model = "fake-model"
        self.provider = "fake"
        self.base_url = "http://fake"
        self.session_id = "sess"
        self.quiet_mode = True
        self.verbose_logging = False
        self.log_prefix = ""
        self.max_iterations = max_iterations
        self.iteration_budget: Any = None
        self.tools: list[dict[str, Any]] = []
        self._api_max_retries = 3
        self._cached_system_prompt: str | None = None
        self._interrupt_requested = False
        self._interrupt_message: str | None = None
        self.prompt_builds = 0
        for key in _SESSION_KEYS:
            setattr(self, f"session_{key}", 0)

    # Prompt and request.
    def _build_system_prompt(self, system_message: str | None) -> str:
        self.prompt_builds += 1
        return "You are a test agent." + (f"\n{system_message}" if system_message else "")

    def _build_api_kwargs(
        self, api_messages: list[dict[str, Any]], tools_for_api: Any = None
    ) -> dict[str, Any]:
        return {"model": self.model, "messages": api_messages, "tools": tools_for_api}

    def _interruptible_api_call(self, api_kwargs: dict[str, Any]) -> Any:
        self.requests.append(api_kwargs)
        response = self.responses.pop(0)
        return response() if callable(response) else response

    def _get_transport(self) -> ChatCompletionsTransport:
        return ChatCompletionsTransport()

    def _copy_reasoning_content_for_api(self, msg: dict[str, Any], api_msg: dict[str, Any]) -> None:
        pass

    # Response handling.
    def _build_assistant_message(
        self, assistant_message: Any, finish_reason: str
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": assistant_message.content or "",
            "finish_reason": finish_reason,
        }
        if assistant_message.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in assistant_message.tool_calls
            ]
        return msg

    def _strip_think_blocks(self, text: str) -> str:
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    def _execute_tool_calls(
        self, assistant_message: Any, messages: list[dict[str, Any]], task_id: str, count: int
    ) -> None:
        execute = (
            tool_executor.execute_tool_calls_concurrent
            if len(assistant_message.tool_calls) > 1
            else tool_executor.execute_tool_calls_sequential
        )
        execute(self, assistant_message, messages, task_id)

    def _handle_max_iterations(self, messages: list[dict[str, Any]], api_call_count: int) -> str:
        self.summaries.append(api_call_count)
        return "Here is what I found so far."

    # Tool executor surface.
    def _invoke_tool(self, *args: Any, **kwargs: Any) -> str:
        return invoke_tool(self, *args, **kwargs)

    def _tool_result_content_for_active_model(self, name: str, content: Any) -> Any:
        return content

    # Output and interrupts.
    def _safe_print(self, *args: Any, **kwargs: Any) -> None:
        pass

    def _vprint(self, *args: Any, **kwargs: Any) -> None:
        pass

    def interrupt(self, message: str | None = None) -> None:
        self._interrupt_requested = True
        self._interrupt_message = message

    def clear_interrupt(self) -> None:
        self._interrupt_requested = False
        self._interrupt_message = None


def _response(
    content: str | None = None,
    tool_calls: list[tuple[str, str, dict[str, Any]]] | None = None,
    usage: tuple[int, int] | None = (10, 5),
) -> SimpleNamespace:
    """An OpenAI ChatCompletion-shaped response; ``tool_calls`` are ``(id, name, args)``."""
    calls = [
        SimpleNamespace(
            id=call_id,
            type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        for call_id, name, args in tool_calls or []
    ]
    message = SimpleNamespace(content=content, tool_calls=calls or None)
    finish_reason = "tool_calls" if calls else "stop"
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(
            prompt_tokens=usage[0], completion_tokens=usage[1], total_tokens=sum(usage)
        )
        if usage
        else None,
    )


def _schema(name: str) -> dict[str, Any]:
    return {"name": name, "description": "", "parameters": {"type": "object", "properties": {}}}


@pytest.fixture
def reg(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    fresh = ToolRegistry()
    monkeypatch.setattr(model_tools, "registry", fresh)
    return fresh


def _register(reg: ToolRegistry, name: str, handler: Callable[..., str]) -> None:
    reg.register(name, "demo", _schema(name), handler)


def _roles(messages: list[dict[str, Any]]) -> list[str]:
    return [m["role"] for m in messages]


# --- design doc §7.4 --------------------------------------------------------------------------


def test_plain_text_answer(reg: ToolRegistry) -> None:
    agent = _Agent([_response("Hello there.")])

    result = run_conversation(agent, "hi")

    assert result["final_response"] == "Hello there."
    assert result["completed"] is True
    assert result["api_calls"] == 1
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert _roles(result["messages"]) == ["user", "assistant"]
    (request,) = agent.requests
    assert request["messages"][0] == {"role": "system", "content": "You are a test agent."}
    assert request["messages"][1]["content"] == "hi"


def test_single_tool_call_then_answer(reg: ToolRegistry) -> None:
    _register(reg, "get_time", lambda args, **kw: "12:00")
    agent = _Agent([_response(tool_calls=[("call_1", "get_time", {})]), _response("It is noon.")])

    result = run_conversation(agent, "what time is it?")

    assert result["final_response"] == "It is noon."
    assert result["api_calls"] == 2
    messages = result["messages"]
    assert _roles(messages) == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["tool_calls"][0]["id"] == "call_1"
    assert messages[2]["tool_call_id"] == "call_1"
    assert messages[2]["content"] == "12:00"
    # The second request carries the tool result back to the model.
    assert agent.requests[1]["messages"][-1]["content"] == "12:00"


def test_parallel_tool_calls_run_together_in_one_round(reg: ToolRegistry) -> None:
    barrier = threading.Barrier(2, timeout=5)

    def meet(args: dict[str, Any], **kwargs: Any) -> str:
        barrier.wait()
        return f"city={args['city']}"

    _register(reg, "weather", meet)
    agent = _Agent(
        [
            _response(
                tool_calls=[("a", "weather", {"city": "Paris"}), ("b", "weather", {"city": "Oslo"})]
            ),
            _response("Both are mild."),
        ]
    )

    result = run_conversation(agent, "weather?")

    tool_rows = [m for m in result["messages"] if m["role"] == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in tool_rows] == [
        ("a", "city=Paris"),
        ("b", "city=Oslo"),
    ]
    assert result["api_calls"] == 2


def test_exhausted_budget_ends_with_a_summary(reg: ToolRegistry) -> None:
    _register(reg, "look", lambda args, **kw: "more to see")
    agent = _Agent(
        [_response(tool_calls=[(f"c{i}", "look", {})]) for i in range(2)], max_iterations=2
    )

    result = run_conversation(agent, "explore")

    assert agent.summaries == [2]
    assert result["final_response"] == "Here is what I found so far."
    assert result["turn_exit_reason"] == "max_iterations_reached(2/2)"
    assert result["completed"] is False
    assert result["messages"][-1] == {
        "role": "assistant",
        "content": "Here is what I found so far.",
        "timestamp": result["messages"][-1]["timestamp"],
    }


def test_stop_mid_turn_leaves_a_history_the_next_turn_can_use(reg: ToolRegistry) -> None:
    agent = _Agent([])

    def slow(args: dict[str, Any], **kwargs: Any) -> str:
        agent.interrupt("stop")
        return "partial"

    _register(reg, "slow", slow)
    agent.responses = [_response(tool_calls=[("c1", "slow", {})])]

    result = run_conversation(agent, "do it")

    assert result["interrupted"] is True
    assert result["completed"] is False
    assert result["turn_exit_reason"] == "interrupted_by_user"
    assert result["interrupt_message"] == "stop"
    assert agent._interrupt_requested is False
    # The tool tail is closed so the next user message does not follow a tool row.
    assert _roles(result["messages"]) == ["user", "assistant", "tool", "assistant"]
    assert result["messages"][-1]["content"] == "Operation interrupted."

    agent.responses = [_response("Picking up again.")]
    history = result["messages"]
    after = run_conversation(agent, "continue", conversation_history=history)

    assert after["final_response"] == "Picking up again."
    assert _roles(after["messages"]) == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    assert len(history) == 4  # the caller's list is copied, not mutated
    assert agent.prompt_builds == 1  # the system prompt is built once and reused


# --- more of the turn -------------------------------------------------------------------------


def test_usage_is_added_up_across_calls(reg: ToolRegistry) -> None:
    _register(reg, "t", lambda args, **kw: "ok")
    agent = _Agent(
        [
            _response(tool_calls=[("c1", "t", {})], usage=(100, 20)),
            _response("done", usage=(130, 7)),
        ]
    )

    result = run_conversation(agent, "go")

    assert result["prompt_tokens"] == 230
    assert result["completion_tokens"] == 27
    assert result["total_tokens"] == 257
    assert agent.session_api_calls == 2


def test_a_response_without_usage_still_counts_the_call(reg: ToolRegistry) -> None:
    agent = _Agent([_response("ok", usage=None)])

    result = run_conversation(agent, "go")

    assert result["total_tokens"] == 0
    assert agent.session_api_calls == 1


def test_think_blocks_are_stripped_from_the_final_response(reg: ToolRegistry) -> None:
    agent = _Agent([_response("<think>hmm</think>The answer.")])

    assert run_conversation(agent, "q")["final_response"] == "The answer."


def test_system_message_is_added_to_the_built_prompt(reg: ToolRegistry) -> None:
    agent = _Agent([_response("ok")])

    run_conversation(agent, "q", system_message="Be brief.")

    assert agent.requests[0]["messages"][0]["content"] == "You are a test agent.\nBe brief."


def test_an_interrupt_before_the_first_call_skips_the_model(reg: ToolRegistry) -> None:
    agent = _Agent([])
    agent._interrupt_requested = True

    result = run_conversation(agent, "q")

    assert agent.requests == []
    assert result["interrupted"] is True
    assert result["api_calls"] == 0


def test_a_processing_error_fills_unanswered_tool_calls_and_ends_the_turn(
    reg: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _Agent([_response(tool_calls=[("c1", "t", {}), ("c2", "t", {})])])

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("executor broke")

    monkeypatch.setattr(agent, "_execute_tool_calls", boom)
    # Step 7 brings AIAgent; the handler only needs its static tool-name reader.
    name_of = staticmethod(lambda tc: tc["function"]["name"])
    monkeypatch.setattr(
        run_agent, "AIAgent", SimpleNamespace(_get_tool_call_name_static=name_of), raising=False
    )

    result = run_conversation(agent, "go")

    assert result["turn_exit_reason"].startswith("local_processing_error(")
    assert result["completed"] is False
    tool_rows = [m for m in result["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_rows] == ["c1", "c2"]
    assert all("executor broke" in m["content"] for m in tool_rows)
