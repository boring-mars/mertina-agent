"""ModelClient.stream through the real SDK and an offline SSE boundary."""

import asyncio
import json

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from mertina_agent.agent.model_client import ModelClient
from mertina_agent.config import Settings
from mertina_agent.exceptions import ModelRequestError, ModelResponseError


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_CUSTOM_HEADERS"):
        monkeypatch.delenv(name, raising=False)


def settings():
    return Settings(
        llm_base_url="https://settings.invalid/v1",
        llm_api_key=SecretStr("settings-key"),
        llm_model="settings-model",
    )


def event(delta=None, finish_reason=None, *, usage=None, choices=None):
    data = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    data["choices"] = (
        choices
        if choices is not None
        else [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}]
    )
    if usage is not None:
        data["usage"] = usage
    return f"data: {json.dumps(data)}\n\n".encode()


DONE = b"data: [DONE]\n\n"
USAGE = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}


class SSEBody(httpx2.AsyncByteStream):
    """An SSE response body that may drop the connection after its events."""

    def __init__(self, events, *, drop=False):
        self.events = events
        self.drop = drop
        self.closed = False

    async def __aiter__(self):
        for item in self.events:
            yield item
        if self.drop:
            message = "connection dropped"
            raise httpx2.ReadError(message)

    async def aclose(self):
        self.closed = True


def run_stream(responses, *, messages=None, tools=(), collect=None):
    """Serve ``responses`` (one per request) and stream once; return (result, requests)."""
    requests = []
    queue = list(responses)

    def handler(request):
        requests.append(request)
        return queue.pop(0)

    deltas, tools_started = collect if collect is not None else ([], [])

    async def scenario():
        async with (
            AsyncOpenAI(
                api_key="injected",
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            ) as external,
            ModelClient(settings(), sdk_client=external) as client,
        ):
            return await client.stream(
                messages or [{"role": "user", "content": "hi"}],
                tools=tools,
                on_text_delta=deltas.append,
                on_tool_started=tools_started.append,
            )

    return asyncio.run(scenario()), requests


def sse(events, *, status=200, drop=False, body=None):
    if body is not None:
        return httpx2.Response(status, json=body)
    return httpx2.Response(
        status, headers={"content-type": "text/event-stream"}, stream=SSEBody(events, drop=drop)
    )


def test_text_is_reported_as_it_streams_and_returned_whole():
    deltas, started = [], []

    result, requests = run_stream(
        [
            sse(
                [
                    event({"content": "Hel"}),
                    event({"content": "lo"}, "stop"),
                    event(choices=[], usage=USAGE),
                    DONE,
                ]
            )
        ],
        collect=(deltas, started),
    )

    assert deltas == ["Hel", "lo"]
    assert result.content == "Hello"
    assert result.usage.total_tokens == 6
    body = json.loads(requests[0].content)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert requests[0].headers["accept"] == "text/event-stream"
    assert requests[0].headers["authorization"] == "Bearer settings-key"


def test_tool_calls_are_announced_and_assembled():
    deltas, started = [], []
    tool_event = event(
        {
            "tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "web_search", "arguments": "{}"}}
            ]
        }
    )

    result, _ = run_stream(
        [sse([tool_event, event({}, "tool_calls"), DONE])],
        tools=[{"type": "function", "function": {"name": "web_search"}}],
        collect=(deltas, started),
    )

    assert started == ["web_search"]
    assert result.tool_calls[0].id == "c1"


def test_http_error_before_the_stream_keeps_its_retry_metadata():
    response = httpx2.Response(
        429, headers={"retry-after": "3"}, json={"error": {"message": "slow"}}
    )

    with pytest.raises(ModelRequestError) as error:
        run_stream([response])

    assert (error.value.kind, error.value.status_code, error.value.retry_after) == (
        "http",
        429,
        "3",
    )


def test_error_event_inside_the_stream_is_a_request_error():
    error_event = b'data: {"error": {"message": "overloaded", "code": 529}}\n\n'

    with pytest.raises(ModelRequestError) as error:
        run_stream([sse([event({"content": "Hi"}), error_event])])

    assert (error.value.kind, str(error.value)) == ("connection", "Model stream reported an error")


def test_connection_dropped_mid_stream_is_a_request_error():
    with pytest.raises(ModelRequestError) as error:
        run_stream([sse([event({"content": "Hi"})], drop=True)])

    assert error.value.kind == "connection"


def test_malformed_event_data_is_a_response_error():
    with pytest.raises(ModelResponseError):
        run_stream([sse([event({"content": "Hi"}), b"data: not-json\n\n"])])


def test_stream_ending_early_is_a_response_error():
    with pytest.raises(ModelResponseError):
        run_stream([sse([event({"content": "Half"}), DONE])])


def test_rejected_stream_options_are_dropped_and_remembered():
    rejection = sse(
        [],
        status=400,
        body={"error": {"message": "stream_options: extra inputs are not permitted"}},
    )
    requests = []
    queue = [
        rejection,
        sse([event({"content": "ok"}, "stop"), DONE]),
        sse([event({"content": "again"}, "stop"), DONE]),
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return queue.pop(0)

    async def scenario():
        async with (
            AsyncOpenAI(
                api_key="injected",
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            ) as external,
            ModelClient(settings(), sdk_client=external) as client,
        ):
            first = await client.stream([{"role": "user", "content": "hi"}])
            second = await client.stream([{"role": "user", "content": "hi"}])
            return first, second

    first, second = asyncio.run(scenario())

    assert (first.content, second.content) == ("ok", "again")
    assert "stream_options" in requests[0]
    assert "stream_options" not in requests[1]
    assert "stream_options" not in requests[2]


def test_other_bad_requests_are_not_retried():
    rejection = sse([], status=400, body={"error": {"message": "model not found"}})

    with pytest.raises(ModelRequestError) as error:
        run_stream([rejection])

    assert error.value.status_code == 400


def test_closed_client_refuses_to_stream():
    async def scenario():
        client = ModelClient(settings())
        await client.aclose()
        await client.stream([{"role": "user", "content": "hi"}])

    with pytest.raises(ModelRequestError) as error:
        asyncio.run(scenario())

    assert error.value.kind == "closed"


def test_stream_without_callbacks_still_returns_the_response():
    def handler(_request):
        return sse([event({"content": "quiet"}, "stop"), DONE])

    async def scenario():
        async with (
            AsyncOpenAI(
                api_key="injected",
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            ) as external,
            ModelClient(settings(), sdk_client=external) as client,
        ):
            return await client.stream([{"role": "user", "content": "hi"}])

    assert asyncio.run(scenario()).content == "quiet"


def test_tool_start_without_a_callback_is_ignored():
    tool_event = event(
        {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "t", "arguments": "{}"}}]}
    )

    def handler(_request):
        return sse([tool_event, event({}, "tool_calls"), DONE])

    async def scenario():
        async with (
            AsyncOpenAI(
                api_key="injected",
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            ) as external,
            ModelClient(settings(), sdk_client=external) as client,
        ):
            return await client.stream([{"role": "user", "content": "hi"}])

    assert asyncio.run(scenario()).tool_calls[0].name == "t"
