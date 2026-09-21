"""Streamed Chat Completions assembly (Hermes stream accumulator intent)."""

import pytest
from openai.types.chat import ChatCompletionChunk

from mertina_agent.agent.transports import (
    ChatCompletionsTransport,
    StreamUpdate,
    ToolCall,
    Usage,
)
from mertina_agent.exceptions import ModelInputError, ModelResponseError


def chunk(delta=None, finish_reason=None, *, usage=None, choices=None):
    data = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    data["choices"] = (
        choices
        if choices is not None
        else [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}]
    )
    if usage is not None:
        data["usage"] = usage
    return data


def tool_delta(index=0, *, identifier=None, name=None, arguments=None):
    function = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    delta = {"index": index, "function": function}
    if identifier is not None:
        delta["id"] = identifier
    return {"tool_calls": [delta]}


def assemble(*chunks):
    accumulator = ChatCompletionsTransport().stream_accumulator()
    updates = [accumulator.feed(item) for item in chunks]
    return updates, accumulator.finish()


USAGE = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def test_text_chunks_are_reported_and_assembled():
    updates, response = assemble(
        chunk({"role": "assistant", "content": "Hel"}),
        chunk({"content": "lo"}, "stop"),
        chunk(choices=[], usage=USAGE),
    )

    assert [update.text for update in updates] == ["Hel", "lo", None]
    assert response.content == "Hello"
    assert response.finish_reason == "stop"
    assert response.usage == Usage(3, 2, 5)


def test_tool_call_fragments_become_one_call_reported_once():
    updates, response = assemble(
        chunk(tool_delta(identifier="call_1", name="web_search", arguments='{"que')),
        chunk(tool_delta(name="web_search", arguments='ry": "x"}')),
        chunk({}, "tool_calls"),
    )

    assert [update.tools_started for update in updates] == [("web_search",), (), ()]
    assert response.tool_calls == (ToolCall("call_1", "web_search", '{"query": "x"}'),)
    assert response.finish_reason == "tool_calls"


def test_text_after_a_tool_call_starts_is_kept_but_not_reported():
    updates, response = assemble(
        chunk({"content": "Let me look. "}),
        chunk(tool_delta(identifier="c", name="web_search", arguments="{}")),
        chunk({"content": "Searching..."}, "tool_calls"),
    )

    assert [update.text for update in updates] == ["Let me look. ", None, None]
    assert response.content == "Let me look. Searching..."


def test_new_id_at_a_reused_index_starts_a_new_call():
    _, response = assemble(
        chunk(tool_delta(0, identifier="a", name="web_search", arguments='{"query": "1"}')),
        chunk(tool_delta(0, identifier="b", name="web_search", arguments='{"query": "2"}')),
        chunk({}, "tool_calls"),
    )

    assert [call.id for call in response.tool_calls] == ["a", "b"]
    assert [call.arguments for call in response.tool_calls] == ['{"query": "1"}', '{"query": "2"}']


def test_missing_index_defaults_to_the_first_slot():
    _, response = assemble(
        chunk({"tool_calls": [{"id": "a", "function": {"name": "t", "arguments": "{}"}}]}),
        chunk({}, "tool_calls"),
    )

    assert response.tool_calls == (ToolCall("a", "t", "{}"),)


def test_refusal_fragments_are_assembled():
    _, response = assemble(chunk({"refusal": "I can't "}), chunk({"refusal": "help."}, "stop"))

    assert response.refusal == "I can't help."
    assert response.content is None


def test_unparseable_arguments_with_a_finish_reason_are_marked_truncated():
    _, response = assemble(
        chunk(tool_delta(identifier="c", name="t", arguments='{"cut": "of')),
        chunk({}, "tool_calls"),
    )

    assert response.finish_reason == "length"


def test_text_without_finish_reason_is_accepted_once_usage_proves_completion():
    _, response = assemble(chunk({"content": "Done."}), chunk(choices=[], usage=USAGE))

    assert response.finish_reason == "stop"


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [chunk({"content": "Half"})],
        [chunk(tool_delta(identifier="c", name="t", arguments='{"a": '))],
        [chunk(tool_delta(identifier="c", name="t"))],
    ],
    ids=["empty", "text-dropped", "arguments-dropped", "name-only-dropped"],
)
def test_dropped_streams_are_errors_not_answers(chunks):
    with pytest.raises(ModelResponseError):
        assemble(*chunks)


def test_tool_call_without_an_id_is_rejected():
    with pytest.raises(ModelResponseError):
        assemble(chunk(tool_delta(name="t", arguments="{}")), chunk({}, "tool_calls"))


@pytest.mark.parametrize(
    "bad_chunk",
    [
        "not a mapping",
        chunk(choices="nope"),
        chunk(choices=[{"delta": {}}, {"delta": {}}]),
        chunk(choices=["not a mapping"]),
        chunk({"content": 7}),
        chunk({"tool_calls": "nope"}),
        chunk({"tool_calls": [{"index": -1}]}),
        chunk({"tool_calls": [{"index": True}]}),
        chunk({"tool_calls": [{"index": 0, "function": "nope"}]}),
        chunk({}, 7),
        chunk(choices=[], usage={"prompt_tokens": -1}),
    ],
    ids=[
        "root",
        "choices-type",
        "two-choices",
        "choice-type",
        "content-type",
        "tool-calls-type",
        "negative-index",
        "bool-index",
        "function-type",
        "finish-type",
        "usage",
    ],
)
def test_malformed_chunks_are_rejected(bad_chunk):
    accumulator = ChatCompletionsTransport().stream_accumulator()

    with pytest.raises(ModelResponseError):
        accumulator.feed(bad_chunk)


def test_sdk_chunk_objects_are_accepted():
    accumulator = ChatCompletionsTransport().stream_accumulator()

    update = accumulator.feed(ChatCompletionChunk.model_validate(chunk({"content": "Hi"}, "stop")))

    assert update == StreamUpdate(text="Hi")
    assert accumulator.finish().content == "Hi"


def test_missing_delta_is_treated_as_empty():
    _, response = assemble(chunk({"content": "x"}), chunk(choices=[{"finish_reason": "stop"}]))

    assert response.finish_reason == "stop"


def test_stream_request_matches_the_non_streaming_request():
    transport = ChatCompletionsTransport()
    tools = [{"type": "function", "function": {"name": "t"}}]
    messages = [{"role": "user", "content": "hi"}]

    streaming = transport.build_stream_kwargs("m", messages, tools)
    plain = transport.build_kwargs("m", messages, tools)

    assert streaming == {
        "model": "m",
        "messages": plain["messages"],
        "tools": plain["tools"],
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def test_stream_request_without_usage_or_tools():
    kwargs = ChatCompletionsTransport().build_stream_kwargs(
        "m", [{"role": "user", "content": "hi"}], include_usage=False
    )

    assert "stream_options" not in kwargs
    assert "tools" not in kwargs


def test_stream_request_validates_its_input():
    with pytest.raises(ModelInputError):
        ChatCompletionsTransport().build_stream_kwargs("m", [])


def test_tool_deltas_may_carry_only_an_id_or_only_arguments():
    _, response = assemble(
        chunk({"tool_calls": [{"index": 0, "id": "c"}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"name": "t"}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}),
        chunk({}, "tool_calls"),
    )

    assert response.tool_calls == (ToolCall("c", "t", "{}"),)


def test_empty_arguments_with_a_finish_reason_are_kept_for_validation():
    _, response = assemble(chunk(tool_delta(identifier="c", name="t")), chunk({}, "tool_calls"))

    assert response.tool_calls == (ToolCall("c", "t", ""),)
    assert response.finish_reason == "tool_calls"
