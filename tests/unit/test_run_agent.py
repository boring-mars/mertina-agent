"""AIAgent end to end: a real agent on a scripted chat-completions client instead of the network."""

import json
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from mertina import model_tools
from mertina.agent import retry_utils, turn_api_error
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
    assert agent.platform is None
    assert agent.pass_session_id is False
    assert agent._tool_use_enforcement == agent._execution_guidance == "auto"
    assert agent.session_start is not None


# --- one turn -------------------------------------------------------------------------------------


def test_chat_returns_the_final_text_and_sends_one_request(reg: ToolRegistry) -> None:
    agent, completions = _agent([_response("Hello there.")], max_tokens=50)

    assert agent.chat("hi") == "Hello there."

    (request,) = completions.requests
    assert request["model"] == "fake-model"
    system, *rest = request["messages"]
    assert system["role"] == "system"
    assert system["content"].startswith("You are Mertina Agent.")
    assert rest == [{"role": "user", "content": "hi"}]
    assert request["max_tokens"] == 50
    assert "tools" not in request


def test_system_message_becomes_the_system_prompt(reg: ToolRegistry) -> None:
    agent, completions = _agent([_response("ok")])

    agent.run_conversation("q", system_message="Be brief.")

    system = completions.requests[0]["messages"][0]
    assert system["role"] == "system"
    identity, context, timestamp = system["content"].split("\n\n")
    assert identity.startswith("You are Mertina Agent.")
    assert context == "Be brief."
    assert timestamp.startswith("Conversation started: ")
    assert timestamp.endswith("\nModel: fake-model")


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


# --- retries --------------------------------------------------------------------------------------

_REQUEST = httpx.Request("POST", "http://fake.test/v1/chat/completions")


def _status_error(
    cls: type[openai.APIStatusError], status: int, headers: dict[str, str] | None = None
) -> openai.APIStatusError:
    response = httpx.Response(status, request=_REQUEST, headers=headers)
    return cls("boom", response=response, body=None)


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry_utils, "jittered_backoff", lambda *args, **kwargs: 0.0)


@pytest.mark.usefixtures("no_backoff")
def test_a_rate_limit_is_retried_and_the_retry_trace_dropped(reg: ToolRegistry) -> None:
    agent, completions = _agent([_status_error(openai.RateLimitError, 429), _response("ok")])

    result = agent.run_conversation("hi")

    assert result["final_response"] == "ok"
    assert len(completions.requests) == 2
    assert agent._retry_status_buffer == []


@pytest.mark.usefixtures("no_backoff")
def test_a_connection_error_is_retried(reg: ToolRegistry) -> None:
    agent, completions = _agent([openai.APIConnectionError(request=_REQUEST), _response("ok")])

    assert agent.run_conversation("hi")["final_response"] == "ok"
    assert len(completions.requests) == 2


def test_retry_after_sets_the_wait(reg: ToolRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    def record(agent: Any, wait_time: float, **kwargs: Any) -> None:
        waits.append(wait_time)

    monkeypatch.setattr(turn_api_error, "interruptible_backoff_sleep", record)
    limited = _status_error(openai.RateLimitError, 429, {"retry-after": "7"})
    agent, _ = _agent([limited, _response("ok")])

    assert agent.run_conversation("hi")["final_response"] == "ok"
    assert waits == [7.0]


@pytest.mark.usefixtures("no_backoff")
def test_server_errors_exhaust_the_retries(reg: ToolRegistry) -> None:
    agent, completions = _agent([_status_error(openai.InternalServerError, 500)] * 3)

    result = agent.run_conversation("hi")

    assert len(completions.requests) == 3
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["failure_reason"] == "server_error"
    assert result["failure_retryable"] is True
    assert result["final_response"] == (
        "The provider returned a server error on all 3 attempts — it looks temporarily "
        "unavailable. Wait a minute and try again.\n\nProvider said: HTTP 500: boom"
    )
    assert agent._retry_status_buffer == []  # flushed on the terminal failure


@pytest.mark.parametrize(
    ("cls", "status", "copy"),
    [
        (openai.BadRequestError, 400, "rejected this request as malformed"),
        (openai.AuthenticationError, 401, "Check the model name, the endpoint and the API key."),
    ],
)
def test_a_client_error_is_not_retried(
    reg: ToolRegistry, cls: type[openai.APIStatusError], status: int, copy: str
) -> None:
    agent, completions = _agent([_status_error(cls, status)], provider="acme")

    result = agent.run_conversation("hi")

    assert len(completions.requests) == 1
    assert result["failed"] is True
    assert result["failure_retryable"] is False
    assert result["final_response"].startswith("acme ")
    assert copy in result["final_response"]


def test_a_stop_during_the_backoff_ends_the_turn_and_the_next_turn_continues(
    reg: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retry_utils, "jittered_backoff", lambda *args, **kwargs: 30.0)
    agent, completions = _agent([_status_error(openai.InternalServerError, 500)])
    stopper = threading.Timer(0.1, agent.interrupt)
    stopper.start()

    result = agent.run_conversation("hi")
    stopper.join()

    assert result["interrupted"] is True
    assert result["final_response"] == (
        "Operation interrupted: retrying API call after error (retry 1/3)."
    )
    assert len(completions.requests) == 1
    assert agent._interrupt_requested is False

    completions.responses = [_response("Back again.")]
    after = agent.run_conversation("again", conversation_history=result["messages"])

    assert after["final_response"] == "Back again."


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
