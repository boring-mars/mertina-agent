"""ChatCompletionsTransport: request building and response normalization."""

from types import SimpleNamespace
from typing import Any

from mertina.agent.transports.chat_completions import ChatCompletionsTransport

_TOOL = {"type": "function", "function": {"name": "get_time", "parameters": {}}}


def _tool_call(extra_content: Any = None) -> dict[str, Any]:
    call: dict[str, Any] = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_time", "arguments": "{}"},
    }
    if extra_content is not None:
        call["extra_content"] = extra_content
    return call


def _response(message: Any, finish_reason: str | None = "stop", usage: Any = None) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)], usage=usage
    )


def _message(**fields: Any) -> Any:
    fields.setdefault("content", None)
    fields.setdefault("tool_calls", None)
    return SimpleNamespace(**fields)


# --- build_kwargs ---------------------------------------------------------------------------------


def test_build_kwargs_carries_model_messages_and_tools() -> None:
    messages = [{"role": "user", "content": "what time is it?"}]

    kwargs = ChatCompletionsTransport().build_kwargs("gpt-4o", messages, tools=[_TOOL])

    assert kwargs == {"model": "gpt-4o", "messages": messages, "tools": [_TOOL]}


def test_build_kwargs_omits_empty_tools() -> None:
    kwargs = ChatCompletionsTransport().build_kwargs("gpt-4o", [], tools=[])

    assert "tools" not in kwargs


def test_build_kwargs_passes_timeout_through() -> None:
    kwargs = ChatCompletionsTransport().build_kwargs("gpt-4o", [], timeout=30)

    assert kwargs["timeout"] == 30


def test_max_tokens_goes_through_the_callers_param_mapping() -> None:
    kwargs = ChatCompletionsTransport().build_kwargs(
        "o3",
        [],
        max_tokens=256,
        max_tokens_param_fn=lambda n: {"max_completion_tokens": n},
    )

    assert kwargs["max_completion_tokens"] == 256
    assert "max_tokens" not in kwargs


def test_max_tokens_without_a_mapping_is_not_sent() -> None:
    kwargs = ChatCompletionsTransport().build_kwargs("gpt-4o", [], max_tokens=256)

    assert "max_tokens" not in kwargs


# --- convert_messages -----------------------------------------------------------------------------


def test_clean_messages_are_returned_unchanged() -> None:
    messages = [{"role": "user", "content": "hi"}]

    assert ChatCompletionsTransport().convert_messages(messages, model="gpt-4o") is messages


def test_internal_and_persistence_keys_are_stripped() -> None:
    messages = [{"role": "user", "content": "hi", "_turn_marker": 1, "timestamp": 123}]

    [sent] = ChatCompletionsTransport().convert_messages(messages, model="gpt-4o")

    assert sent == {"role": "user", "content": "hi"}


def test_stripping_does_not_mutate_the_stored_history() -> None:
    stored = {"role": "user", "content": "hi", "_turn_marker": 1}

    ChatCompletionsTransport().convert_messages([stored], model="gpt-4o")

    assert stored["_turn_marker"] == 1


def test_name_is_stripped_from_tool_results_only() -> None:
    messages = [
        {"role": "user", "content": "hi", "name": "alice"},
        {"role": "tool", "tool_call_id": "call_1", "content": "12:00", "name": "get_time"},
    ]

    user, tool = ChatCompletionsTransport().convert_messages(messages, model="gpt-4o")

    assert user["name"] == "alice"
    assert "name" not in tool


def test_empty_assistant_tool_calls_are_dropped() -> None:
    messages = [{"role": "assistant", "content": "done", "tool_calls": []}]

    [sent] = ChatCompletionsTransport().convert_messages(messages, model="gpt-4o")

    assert "tool_calls" not in sent


def test_reasoning_details_never_go_on_the_wire() -> None:
    messages = [{"role": "assistant", "content": "x", "reasoning_details": [{"type": "text"}]}]

    [sent] = ChatCompletionsTransport().convert_messages(messages, model="gpt-4o")

    assert "reasoning_details" not in sent


def test_thought_signature_is_stripped_for_non_gemini_models() -> None:
    call = _tool_call({"google": {"thought_signature": "sig"}})
    messages = [{"role": "assistant", "content": None, "tool_calls": [call]}]

    [sent] = ChatCompletionsTransport().convert_messages(messages, model="gpt-4o")

    assert "extra_content" not in sent["tool_calls"][0]
    assert "extra_content" in call


def test_thought_signature_is_replayed_to_gemini() -> None:
    call = _tool_call({"google": {"thought_signature": "sig"}})
    messages = [{"role": "assistant", "content": None, "tool_calls": [call]}]

    result = ChatCompletionsTransport().convert_messages(messages, model="gemini-2.5-pro")

    assert result is messages


def test_empty_thought_signature_is_not_replayed_even_to_gemini() -> None:
    call = _tool_call({"google": {"thought_signature": "  "}})
    messages = [{"role": "assistant", "content": None, "tool_calls": [call]}]

    [sent] = ChatCompletionsTransport().convert_messages(messages, model="gemini-2.5-pro")

    assert "extra_content" not in sent["tool_calls"][0]


# --- normalize_response ---------------------------------------------------------------------------


def test_plain_text_answer() -> None:
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=3, total_tokens=13)

    result = ChatCompletionsTransport().normalize_response(
        _response(_message(content="It is noon."), usage=usage)
    )

    assert result.content == "It is noon."
    assert result.tool_calls is None
    assert result.finish_reason == "stop"
    assert result.usage is not None
    assert result.usage.total_tokens == 13


def test_missing_finish_reason_defaults_to_stop() -> None:
    result = ChatCompletionsTransport().normalize_response(
        _response(_message(content="hi"), finish_reason=None)
    )

    assert result.finish_reason == "stop"


def test_tool_calls_are_normalized() -> None:
    raw_call = SimpleNamespace(
        id="call_1", function=SimpleNamespace(name="get_time", arguments='{"tz": "UTC"}')
    )

    result = ChatCompletionsTransport().normalize_response(
        _response(_message(tool_calls=[raw_call]), finish_reason="tool_calls")
    )

    assert result.finish_reason == "tool_calls"
    assert result.tool_calls is not None
    [call] = result.tool_calls
    assert (call.id, call.name, call.arguments) == ("call_1", "get_time", '{"tz": "UTC"}')
    assert call.provider_data is None


def test_tool_call_without_arguments_gets_an_empty_object() -> None:
    raw_call = SimpleNamespace(
        id="call_1", function=SimpleNamespace(name="get_time", arguments=None)
    )

    result = ChatCompletionsTransport().normalize_response(
        _response(_message(tool_calls=[raw_call]))
    )

    assert result.tool_calls is not None
    assert result.tool_calls[0].arguments == "{}"


def test_tool_call_without_a_name_is_dropped() -> None:
    raw_call = SimpleNamespace(id="call_1", function=SimpleNamespace(name=None, arguments="{}"))

    result = ChatCompletionsTransport().normalize_response(
        _response(_message(tool_calls=[raw_call]))
    )

    assert result.tool_calls == []


def test_thought_signature_rides_on_the_tool_call() -> None:
    extra = {"google": {"thought_signature": "sig"}}
    raw_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="get_time", arguments="{}"),
        extra_content=extra,
    )

    result = ChatCompletionsTransport().normalize_response(
        _response(_message(tool_calls=[raw_call]))
    )

    assert result.tool_calls is not None
    assert result.tool_calls[0].extra_content == extra


def test_reasoning_content_is_read_from_model_extra() -> None:
    message = _message(content="42", model_extra={"reasoning_content": "thinking..."})

    result = ChatCompletionsTransport().normalize_response(_response(message))

    assert result.reasoning_content == "thinking..."


def test_a_refusal_alone_becomes_the_content_with_content_filter() -> None:
    result = ChatCompletionsTransport().normalize_response(
        _response(_message(content=None, refusal="I cannot help with that."))
    )

    assert result.content == "I cannot help with that."
    assert result.finish_reason == "content_filter"


def test_a_refusal_next_to_real_content_keeps_the_content() -> None:
    result = ChatCompletionsTransport().normalize_response(
        _response(_message(content="Partly: here.", refusal="Not the rest."))
    )

    assert result.content == "Partly: here."
    assert result.finish_reason == "stop"


# --- validate_response / extract_cache_stats ------------------------------------------------------


def test_validate_response_requires_choices() -> None:
    transport = ChatCompletionsTransport()

    assert not transport.validate_response(None)
    assert not transport.validate_response(SimpleNamespace(choices=[]))
    assert transport.validate_response(_response(_message(content="hi")))


def test_cache_stats_from_openai_style_usage() -> None:
    usage = SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=80))

    stats = ChatCompletionsTransport().extract_cache_stats(SimpleNamespace(usage=usage))

    assert stats == {"cached_tokens": 80, "creation_tokens": 0}


def test_cache_stats_from_deepseek_usage() -> None:
    usage = SimpleNamespace(prompt_tokens_details=None, prompt_cache_hit_tokens=64)

    stats = ChatCompletionsTransport().extract_cache_stats(SimpleNamespace(usage=usage))

    assert stats == {"cached_tokens": 64, "creation_tokens": 0}


def test_no_cache_stats_without_cache_hits() -> None:
    usage = SimpleNamespace(prompt_tokens_details=None)

    assert ChatCompletionsTransport().extract_cache_stats(SimpleNamespace(usage=usage)) is None
