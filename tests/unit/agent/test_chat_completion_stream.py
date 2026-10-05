"""The streaming request: chunk assembly, drop handling and the interrupt poll."""

import threading
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from mertina.agent.chat_completion_helpers import (
    _StreamingCall,
    _ToolCallAccumulator,
    interruptible_streaming_api_call,
)
from mertina.agent.errors import EmptyStreamError
from mertina.agent.stream_delivery import StreamDeliveryMixin
from mertina.agent.think_scrubber import StreamingThinkScrubber
from mertina.constants import FINISH_REASON_LENGTH, PARTIAL_STREAM_STUB_ID


class _Agent(StreamDeliveryMixin):
    """The agent surface a stream touches; each request takes the next scripted stream."""

    def __init__(self, streams: list[Callable[[], Iterator[Any]]]) -> None:
        self.streams = list(streams)
        self.requests: list[dict[str, Any]] = []
        self.events: list[tuple[str, str]] = []
        self.deltas: list[str] = []
        self.tool_names: list[str] = []
        self.diagnostics: list[str] = []
        self.model = "fake-model"
        self._interrupt_requested = False
        self._stream_options_unsupported = False
        self._stream_needs_break = False
        self._stream_callback = None
        self._stream_think_scrubber = StreamingThinkScrubber()
        self.stream_delta_callback = self.deltas.append
        self.tool_gen_callback = self.tool_names.append
        self._strip_think_blocks = lambda text: text
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=self._create))
        )

    def _create(self, **kwargs: Any) -> Iterator[Any]:
        self.requests.append(kwargs)
        return self.streams.pop(0)()

    def _create_request_openai_client(self, *, reason: str) -> Any:
        return self.client

    def _close_request_openai_client(self, client: Any, *, reason: str) -> None:
        self.events.append(("close", reason))

    def _abort_request_openai_client(self, client: Any, *, reason: str) -> None:
        self.events.append(("abort", reason))

    def _buffer_diagnostic_status(self, message: str) -> None:
        self.diagnostics.append(message)


def _chunk(
    content: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list[Any] | None = None,
    **extra: Any,
) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)], **extra
    )


def _call(
    name: str | None, arguments: str | None, index: int = 0, call_id: str | None = None
) -> Any:
    return SimpleNamespace(
        index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def _stream(*chunks: Any, error: Exception | None = None) -> Callable[[], Iterator[Any]]:
    def stream() -> Iterator[Any]:
        yield from chunks
        if error is not None:
            raise error

    return stream


def test_a_streamed_reply_is_assembled_into_the_non_streaming_shape() -> None:
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    agent = _Agent(
        [
            _stream(
                _chunk("Hi", id="chatcmpl-1", model="m-1"),
                _chunk(" there."),
                _chunk(finish_reason="stop"),
                SimpleNamespace(choices=[], usage=usage),
            )
        ]
    )

    response = interruptible_streaming_api_call(agent, {"model": "fake-model", "messages": []})

    assert agent.deltas == ["Hi", " there."]
    assert response.id == "chatcmpl-1"
    assert response.model == "m-1"
    assert response.usage is usage
    (choice,) = response.choices
    assert choice.finish_reason == "stop"
    assert choice.message.content == "Hi there."
    assert choice.message.tool_calls is None
    (request,) = agent.requests
    assert request["stream"] is True
    assert isinstance(request["timeout"], httpx.Timeout)
    assert agent.events == [("close", "stream_request_complete")]


def test_tool_call_deltas_are_joined_and_text_beside_them_goes_straight_to_the_callback() -> None:
    agent = _Agent(
        [
            _stream(
                _chunk(tool_calls=[_call("lookup", None, call_id="c1")]),
                _chunk("thinking aloud", tool_calls=[_call(None, '{"q": ')]),
                _chunk(tool_calls=[_call(None, '"x"}')]),
                _chunk(finish_reason="tool_calls"),
            )
        ]
    )

    response = interruptible_streaming_api_call(agent, {"model": "fake-model"})

    assert agent.tool_names == ["lookup"]
    # Raw, for the display to pull reasoning tags from; the paragraph and scrub path is skipped.
    assert agent.deltas == ["thinking aloud"]
    (call,) = response.choices[0].message.tool_calls
    assert (call.id, call.function.name, call.function.arguments) == ("c1", "lookup", '{"q": "x"}')


def test_a_reused_index_with_a_new_id_starts_a_new_call() -> None:
    acc = _ToolCallAccumulator()

    assert acc.feed(_call("a", "{}", index=0, call_id="c1")) == "a"
    assert acc.feed(_call("b", "{}", index=0, call_id="c2")) == "b"
    assert acc.feed(_call("b", None, index=0)) is None  # a resent name is not announced twice

    calls = acc.materialize()
    assert [(c["id"], c["function"]["name"]) for c in calls.values()] == [("c1", "a"), ("c2", "b")]


def test_unparseable_arguments_mark_the_reply_truncated() -> None:
    calls, truncated = _StreamingCall._assemble_tool_calls(
        {0: {"id": "c1", "type": "function", "function": {"name": "t", "arguments": '{"a": '}}},
        "stop",
    )

    assert truncated is True
    assert calls is not None and calls[0].function.arguments == '{"a": '


@pytest.mark.parametrize(
    "chunks",
    [
        # arguments cut off and no finish_reason: a drop mid tool call
        (_chunk(tool_calls=[_call("t", '{"a": ', call_id="c1")]),),
        # text and no finish_reason or usage: a drop mid reply
        (_chunk("Half a"),),
    ],
)
def test_a_stream_that_ends_without_a_finish_reason_becomes_a_partial_stub(
    chunks: tuple[Any, ...],
) -> None:
    agent = _Agent([_stream(*chunks)])

    response = interruptible_streaming_api_call(agent, {"model": "fake-model"})

    assert response.id == PARTIAL_STREAM_STUB_ID
    assert response.choices[0].finish_reason == FINISH_REASON_LENGTH
    assert response.choices[0].message.tool_calls is None


def test_an_empty_stream_is_retried_and_then_reported() -> None:
    agent = _Agent([_stream()] * 3)

    with pytest.raises(EmptyStreamError):
        interruptible_streaming_api_call(agent, {"model": "fake-model"})

    assert len(agent.requests) == 3  # MERTINA_STREAM_RETRIES defaults to 2 retries
    assert agent.diagnostics and "empty response stream after 3 attempts" in agent.diagnostics[0]


def test_an_error_that_is_not_a_drop_is_raised_without_a_retry() -> None:
    agent = _Agent([_stream(error=ValueError("bad payload"))])

    with pytest.raises(ValueError, match="bad payload"):
        interruptible_streaming_api_call(agent, {"model": "fake-model"})

    assert len(agent.requests) == 1


def test_a_drop_mid_tool_call_after_text_is_retried() -> None:
    agent = _Agent(
        [
            _stream(
                _chunk("Let me check."),
                _chunk(tool_calls=[_call("t", None, call_id="c1")]),
                error=httpx.ReadError("reset"),
            ),
            _stream(
                _chunk("Let me check."),
                _chunk(tool_calls=[_call("t", "{}", call_id="c1")]),
                _chunk(finish_reason="tool_calls"),
            ),
        ]
    )

    response = interruptible_streaming_api_call(agent, {"model": "fake-model"})

    assert len(agent.requests) == 2  # the tool call would be lost, so the preamble repeats
    assert response.choices[0].message.tool_calls[0].function.name == "t"


def test_a_stop_seen_between_chunks_closes_the_stream() -> None:
    agent = _Agent([])
    stopped = threading.Event()

    def stream() -> Iterator[Any]:
        yield _chunk("Hel")
        agent._interrupt_requested = True
        yield _chunk("lo")
        stopped.wait(timeout=5)  # never reached: the worker stops reading
        yield _chunk(finish_reason="stop")

    agent.streams.append(stream)

    with pytest.raises(InterruptedError):
        interruptible_streaming_api_call(agent, {"model": "fake-model"})
    stopped.set()

    assert agent.deltas == ["Hel"]


def test_a_stop_before_the_request_raises_at_once() -> None:
    agent = _Agent([])
    agent._interrupt_requested = True

    with pytest.raises(InterruptedError):
        interruptible_streaming_api_call(agent, {"model": "fake-model"})

    assert agent.requests == []
