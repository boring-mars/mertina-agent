"""AIAgent end to end: a real agent on a scripted chat-completions client instead of the network."""

import json
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from mertina import model_tools
from mertina.agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST
from mertina.run_agent import AIAgent
from mertina.tools.registry import ToolRegistry


class _Completions:
    """``client.chat.completions`` that records each request and returns scripted responses."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response() if callable(response) else response


class _Client:
    def __init__(self, responses: list[Any]) -> None:
        self.chat = SimpleNamespace(completions=_Completions(responses))

    def close(self) -> None:
        pass


def _response(
    content: str | None = None,
    tool_calls: list[tuple[str, str, dict[str, Any]]] | None = None,
    usage: tuple[int, int] | None = (10, 5),
    reasoning_content: str | None = None,
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
    message = SimpleNamespace(
        content=content, tool_calls=calls or None, reasoning_content=reasoning_content
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if calls else "stop")],
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


def _agent(responses: list[Any], **kwargs: Any) -> tuple[Any, _Completions]:
    """An AIAgent (typed ``Any``: ``init_agent`` sets its attributes) on a scripted client."""
    kwargs.setdefault("base_url", "http://fake.test/v1")
    agent = AIAgent(api_key="test-key", model="fake-model", quiet_mode=True, **kwargs)
    client = _Client(responses)
    agent.client = client
    return agent, client.chat.completions


def _roles(messages: list[dict[str, Any]]) -> list[str]:
    return [m["role"] for m in messages]


# --- construction ---------------------------------------------------------------------------------


def test_construction_records_the_settings_the_loop_reads(reg: ToolRegistry) -> None:
    _register(reg, "get_time", lambda args, **kw: "12:00")

    agent: Any = AIAgent(
        base_url="http://fake.test/v1?api-version=1",
        api_key="k",
        provider=" Custom ",
        model="m",
        max_iterations=7,
        quiet_mode=True,
    )

    assert agent.api_mode == "chat_completions"
    assert agent.provider == "custom"
    assert agent.base_url == "http://fake.test/v1"
    assert agent._client_kwargs["default_query"] == {"api-version": "1"}
    assert agent.valid_tool_names == {"get_time"}
    assert agent.iteration_budget.max_total == 7
    assert agent._api_max_retries == 3
    assert agent.session_api_calls == 0


# --- one turn -------------------------------------------------------------------------------------


def test_chat_returns_the_final_text_and_sends_one_request(reg: ToolRegistry) -> None:
    agent, completions = _agent([_response("Hello there.")], max_tokens=50)

    assert agent.chat("hi") == "Hello there."

    (request,) = completions.requests
    assert request["model"] == "fake-model"
    assert request["messages"] == [{"role": "user", "content": "hi"}]
    assert request["max_tokens"] == 50
    assert "tools" not in request


def test_system_message_becomes_the_system_prompt(reg: ToolRegistry) -> None:
    agent, completions = _agent([_response("ok")])

    agent.run_conversation("q", system_message="Be brief.")

    assert completions.requests[0]["messages"][0] == {"role": "system", "content": "Be brief."}


def test_tool_round_trip_with_parallel_calls(reg: ToolRegistry) -> None:
    barrier = threading.Barrier(2, timeout=5)

    def weather(args: dict[str, Any], **kwargs: Any) -> str:
        barrier.wait()
        return f"mild in {args['city']}"

    _register(reg, "weather", weather)
    agent, completions = _agent(
        [
            _response(
                tool_calls=[("a", "weather", {"city": "Paris"}), ("b", "weather", {"city": "Oslo"})]
            ),
            _response("Both are mild."),
        ]
    )

    result = agent.run_conversation("weather?")

    assert result["final_response"] == "Both are mild."
    assert result["completed"] is True
    assert _roles(result["messages"]) == ["user", "assistant", "tool", "tool", "assistant"]
    assert [c["id"] for c in result["messages"][1]["tool_calls"]] == ["a", "b"]
    sent = completions.requests[1]
    assert [t["function"]["name"] for t in sent["tools"]] == ["weather"]
    assert [m["content"] for m in sent["messages"] if m["role"] == "tool"] == [
        "mild in Paris",
        "mild in Oslo",
    ]


def test_usage_is_summed_into_the_result(reg: ToolRegistry) -> None:
    _register(reg, "t", lambda args, **kw: "ok")
    agent, _ = _agent(
        [
            _response(tool_calls=[("c1", "t", {})], usage=(100, 20)),
            _response("done", usage=(130, 7)),
        ]
    )

    result = agent.run_conversation("go")

    assert (result["prompt_tokens"], result["completion_tokens"], result["total_tokens"]) == (
        230,
        27,
        257,
    )
    assert agent.session_api_calls == 2


def test_think_blocks_are_stripped_and_kept_as_reasoning(reg: ToolRegistry) -> None:
    agent, _ = _agent([_response("<think>check the date</think>It is Monday.")])

    result = agent.run_conversation("day?")

    assert result["final_response"] == "It is Monday."
    assert result["last_reasoning"] == "check the date"
    assert result["messages"][-1]["content"] == "It is Monday."


# --- budget, interrupts, failures -----------------------------------------------------------------


def test_exhausted_budget_asks_the_model_for_a_summary(reg: ToolRegistry) -> None:
    _register(reg, "look", lambda args, **kw: "more to see")
    agent, completions = _agent(
        [_response(tool_calls=[("c1", "look", {})]), _response("Here is the summary.")],
        max_iterations=1,
    )

    result = agent.run_conversation("explore")

    assert result["final_response"] == "Here is the summary."
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert result["completed"] is False
    summary_request = completions.requests[1]
    assert summary_request["messages"][-1]["content"] == MAX_ITERATIONS_SUMMARY_REQUEST
    assert result["messages"][-1]["content"] == "Here is the summary."


def test_a_failed_summary_returns_the_out_of_steps_copy(reg: ToolRegistry) -> None:
    _register(reg, "look", lambda args, **kw: "more to see")
    agent, _ = _agent(
        [_response(tool_calls=[("c1", "look", {})]), RuntimeError("down")], max_iterations=1
    )

    result = agent.run_conversation("explore")

    assert "ran out of steps for this turn (1 tool calls)" in result["final_response"]


def test_an_interrupt_during_a_tool_ends_the_turn_and_the_next_turn_continues(
    reg: ToolRegistry,
) -> None:
    holder: dict[str, AIAgent] = {}

    def slow(args: dict[str, Any], **kwargs: Any) -> str:
        holder["agent"].interrupt("stop")
        return "partial"

    _register(reg, "slow", slow)
    agent, completions = _agent([_response(tool_calls=[("c1", "slow", {})])])
    holder["agent"] = agent

    result = agent.run_conversation("do it")

    assert result["interrupted"] is True
    assert result["interrupt_message"] == "stop"
    assert agent._interrupt_requested is False
    assert _roles(result["messages"]) == ["user", "assistant", "tool", "assistant"]

    completions.responses = [_response("Picking up again.")]
    after = agent.run_conversation("continue", conversation_history=result["messages"])

    assert after["final_response"] == "Picking up again."
    assert _roles(after["messages"])[-2:] == ["user", "assistant"]


# --- reasoning_content echo-back ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "echoed"),
    [("https://api.deepseek.com/v1", True), ("https://api.mistral.ai/v1", False)],
)
def test_reasoning_content_is_replayed_only_where_the_provider_requires_it(
    reg: ToolRegistry, base_url: str, echoed: bool
) -> None:
    _register(reg, "t", lambda args, **kw: "ok")
    agent, completions = _agent(
        [
            _response(tool_calls=[("c1", "t", {})], reasoning_content="plan the call"),
            _response("done"),
        ],
        base_url=base_url,
    )

    agent.run_conversation("go")

    replayed = [m for m in completions.requests[1]["messages"] if m["role"] == "assistant"]
    assert ("reasoning_content" in replayed[0]) is echoed
    if echoed:
        assert replayed[0]["reasoning_content"] == "plan the call"
