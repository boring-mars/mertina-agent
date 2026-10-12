from types import SimpleNamespace as NS

from agent.web_search_provider import WebSearchProvider
from agent.web_search_registry import register_provider
from run_agent import AIAgent


def response(content=None, tool_calls=None, finish_reason="stop", usage=None):
    message = NS(
        role="assistant", content=content, tool_calls=tool_calls,
        reasoning_content=None, reasoning=None, refusal=None,
    )
    return NS(
        choices=[NS(message=message, finish_reason=finish_reason)],
        usage=usage, model="fake-model",
    )


def tool_call(call_id, query):
    return NS(
        id=call_id, type="function",
        function=NS(name="web_search", arguments=f'{{"query":"{query}"}}'),
    )


class FakeSearchProvider(WebSearchProvider):
    name = "gateway"

    def is_available(self):
        return True

    def search(self, query, limit=5):
        return {"success": True, "data": {"web": [{
            "title": query, "url": f"https://example.com/{query}",
            "description": "result", "position": 1,
        }]}}


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.chat = NS(completions=FakeCompletions(outcomes))
        self.api_key = "test-key"
        self.base_url = "http://test/v1"
        self.is_closed = False

    def close(self):
        self.is_closed = True


def make_agent(outcomes, *, max_iterations=5, **kwargs):
    agent = AIAgent(
        base_url="http://test/v1", api_key="test-key", model="fake-model",
        max_iterations=max_iterations, quiet_mode=True, **kwargs,
    )
    client = FakeClient(outcomes)
    agent.client = client
    agent._create_request_openai_client = lambda **_kwargs: client
    agent._disable_streaming = True
    register_provider(FakeSearchProvider())
    return agent, client


def test_plain_answer_and_usage():
    usage = NS(
        prompt_tokens=3, completion_tokens=2, total_tokens=5,
        prompt_tokens_details=None, completion_tokens_details=None,
    )
    agent, _ = make_agent([response("hello", usage=usage)])

    result = agent.run_conversation("hi")

    assert result["final_response"] == "hello"
    assert result["completed"] is True
    assert result["total_tokens"] == 5


def test_chat_returns_final_text():
    agent, _ = make_agent([response("hello from chat")])

    assert agent.chat("hi") == "hello from chat"


def test_tool_call_then_answer():
    agent, client = make_agent([
        response(tool_calls=[tool_call("call_1", "alpha")], finish_reason="tool_calls"),
        response("done"),
    ])

    result = agent.run_conversation("search")

    assert result["final_response"] == "done"
    assert [message["role"] for message in result["messages"]] == [
        "user", "assistant", "tool", "assistant",
    ]
    assert "https://example.com/alpha" in result["messages"][2]["content"]
    assert len(client.chat.completions.calls) == 2


def test_parallel_tool_calls_keep_result_order():
    agent, _ = make_agent([
        response(tool_calls=[
            tool_call("call_1", "first"), tool_call("call_2", "second"),
        ], finish_reason="tool_calls"),
        response("done"),
    ])

    result = agent.run_conversation("search twice")
    tools = [message for message in result["messages"] if message["role"] == "tool"]

    assert [message["tool_call_id"] for message in tools] == ["call_1", "call_2"]
    assert "example.com/first" in tools[0]["content"]
    assert "example.com/second" in tools[1]["content"]


def test_iteration_budget_stops_after_tool_result():
    agent, client = make_agent([
        response(tool_calls=[tool_call("call_1", "limit")], finish_reason="tool_calls"),
    ], max_iterations=1)

    result = agent.run_conversation("search")

    assert result["completed"] is False
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert len(client.chat.completions.calls) == 1
    assert result["messages"][-1]["role"] == "tool"


class ApiFailure(Exception):
    def __init__(self, status_code, message="failed", headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = NS(headers=headers or {})


def test_retry_server_error_then_succeed(monkeypatch):
    monkeypatch.setattr("agent.turn_api_error.time.sleep", lambda _seconds: None)
    agent, client = make_agent([ApiFailure(503), response("recovered")])

    result = agent.run_conversation("retry")

    assert result["final_response"] == "recovered"
    assert result["completed"] is True
    assert len(client.chat.completions.calls) == 2


def test_client_error_is_not_retried(monkeypatch):
    monkeypatch.setattr("agent.turn_api_error.time.sleep", lambda _seconds: None)
    agent, client = make_agent([ApiFailure(400, "bad request")])

    result = agent.run_conversation("fail")

    assert result["failed"] is True
    assert result["failure_retryable"] is False
    assert len(client.chat.completions.calls) == 1


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.response = None
        self.final_response = None
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


def stream_chunk(text, finish_reason=None):
    delta = NS(
        role="assistant", content=text, tool_calls=None, reasoning_content=None,
        reasoning=None, reasoning_details=None, refusal=None, model_extra=None,
    )
    return NS(
        choices=[NS(delta=delta, finish_reason=finish_reason)], usage=None,
        model="fake-model", id="response_1", provider=None,
    )


def stream_tool_chunk(call_id, query, finish_reason="tool_calls"):
    delta = NS(
        role="assistant", content=None, reasoning_content=None, reasoning=None,
        reasoning_details=None, refusal=None, model_extra=None,
        tool_calls=[NS(
            index=0, id=call_id, type="function", extra_content=None,
            function=NS(name="web_search", arguments=f'{{"query":"{query}"}}'),
        )],
    )
    return NS(
        choices=[NS(delta=delta, finish_reason=finish_reason)], usage=None,
        model="fake-model", id="response_tool", provider=None,
    )


def test_streaming_tool_progress_then_answer():
    started = []
    stream = FakeStream([stream_tool_chunk("call_stream", "streamed")])
    agent, _ = make_agent(
        [stream, FakeStream([stream_chunk("done", "stop")])],
        tool_gen_callback=started.append,
    )
    agent._disable_streaming = False

    result = agent.run_conversation("search while streaming")

    assert started == ["web_search"]
    assert result["final_response"] == "done"
    assert "example.com/streamed" in result["messages"][2]["content"]


def test_stop_mid_stream_and_continue_with_returned_history():
    stream = FakeStream([stream_chunk("partial"), stream_chunk(" ignored", "stop")])
    holder = {}
    agent, client = make_agent([stream], stream_delta_callback=lambda text: holder["agent"].interrupt("stop") if text else None)
    holder["agent"] = agent
    agent._disable_streaming = False

    stopped = agent.run_conversation("start")

    assert stopped["interrupted"] is True
    assert stopped["final_response"] == "partial"
    assert stream.closed is True

    client.chat.completions.outcomes = iter([response("continued")])
    agent._disable_streaming = True
    continued = agent.run_conversation("continue", conversation_history=stopped["messages"])

    assert continued["final_response"] == "continued"
    assert continued["completed"] is True
