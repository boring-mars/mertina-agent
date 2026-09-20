"""Offline T01-T12 cases; upstream regression intent is recorded in source notes."""

from copy import deepcopy

import pytest
from openai.types.chat import ChatCompletion

from mertina_agent.agent.transports import ChatCompletionsTransport, ToolCall, Usage
from mertina_agent.exceptions import ModelInputError, ModelResponseError


@pytest.fixture
def transport():
    return ChatCompletionsTransport()


def response(*, content="hello", finish_reason="stop", **message_fields):
    return {
        "id": "completion-test",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, **message_fields},
            }
        ],
    }


def wire_call(*, identifier="call_1", name="lookup", arguments='{"query":"test"}'):
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def tool_definition(**function_fields):
    return {"type": "function", "function": {"name": "lookup", **function_fields}}


def test_minimal_request_has_no_implicit_parameters(transport):
    messages = [{"role": "user", "content": "hello"}]
    assert transport.build_kwargs("test-model", messages) == {
        "model": "test-model",
        "messages": messages,
        "stream": False,
    }


def test_minimal_function_definition_does_not_fabricate_optional_fields(transport):
    tools = [tool_definition()]
    assert transport.convert_tools(tools) == tools


@pytest.mark.parametrize("field", ["id", "type", "function", "name", "arguments"])
def test_missing_required_call_fields_fail_for_history_and_response(transport, field):
    call = wire_call()
    if field in {"name", "arguments"}:
        del call["function"][field]
    else:
        del call[field]
    with pytest.raises(ModelInputError):
        transport.convert_messages([{"role": "assistant", "tool_calls": [call]}])
    with pytest.raises(ModelResponseError):
        transport.normalize_response(
            response(content=None, finish_reason="tool_calls", tool_calls=[call])
        )


def test_history_roles_and_tool_correlation_are_preserved(transport):
    messages = [
        {"role": "system", "content": "system instruction"},
        {"role": "developer", "content": "developer instruction"},
        {"role": "user", "content": "query"},
        {"role": "assistant", "content": None, "tool_calls": [wire_call()]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "answer"},
    ]
    result = transport.build_kwargs("gpt-5", messages)
    assert result["messages"] == messages
    assert set(result["messages"][4]) == {"role", "tool_call_id", "content"}


@pytest.mark.parametrize("calls", [[], (), None])
def test_empty_assistant_tool_calls_are_omitted(transport, calls):
    messages = [{"role": "assistant", "content": "answer", "tool_calls": calls}]
    assert transport.convert_messages(messages) == [{"role": "assistant", "content": "answer"}]
    assert "tool_calls" in messages[0]


def test_tool_only_and_refusal_only_history_allow_missing_content(transport):
    messages = [
        {"role": "assistant", "tool_calls": [wire_call()]},
        {"role": "assistant", "refusal": "Cannot comply."},
    ]
    assert transport.convert_messages(messages) == messages


def test_nested_inputs_are_detached_without_mutation(transport):
    messages = [{"role": "assistant", "tool_calls": [wire_call()]}]
    tools = [
        tool_definition(
            description="Look up text",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "enum": ["a", "b"]}},
                "required": ["query"],
                "additionalProperties": False,
                "examples": [{"query": "a"}],
            },
        )
    ]
    original_messages, original_tools = deepcopy(messages), deepcopy(tools)
    built = transport.build_kwargs("model", messages, tools)
    assert messages == original_messages
    assert tools == original_tools
    built["messages"][0]["tool_calls"][0]["function"]["arguments"] = "changed"
    built["tools"][0]["function"]["parameters"]["properties"]["query"]["enum"].append("c")
    assert messages == original_messages
    assert tools == original_tools


@pytest.mark.parametrize(
    "messages",
    [
        None,
        "not-a-list",
        {},
        [],
        [None],
        [{"role": "unknown", "content": "text"}],
        [{"role": [], "content": "text"}],
        [{"role": "user", "content": [{"type": "text", "text": "unsupported"}]}],
        [{"role": "user"}],
        [{"role": "user", "content": "text", "internal_secret": "hidden"}],
        [{"role": "user", "content": "text", "tool_calls": []}],
        [{"role": "assistant", "content": None}],
        [{"role": "assistant", "content": [], "tool_calls": [wire_call()]}],
        [{"role": "assistant", "content": "text", "refusal": {}}],
        [{"role": "assistant", "tool_calls": "bad"}],
        [{"role": "assistant", "tool_calls": [None]}],
        [{"role": "assistant", "tool_calls": [{"type": "custom"}]}],
        [{"role": "assistant", "tool_calls": [wire_call(identifier=" ")]}],
        [{"role": "assistant", "tool_calls": [wire_call(name="")]}],
        [{"role": "assistant", "tool_calls": [wire_call(arguments={})]}],
        [{"role": "tool", "content": "text"}],
        [{"role": "tool", "content": "text", "tool_call_id": ""}],
        [{"role": "tool", "content": "text", "tool_call_id": "call_1", "name": "lookup"}],
    ],
)
def test_invalid_messages_are_rejected_without_silent_data_loss(transport, messages):
    with pytest.raises(ModelInputError):
        transport.build_kwargs("model", messages)


@pytest.mark.parametrize(
    "tools",
    [
        None,
        "bad",
        [None],
        [{"type": "custom", "function": {"name": "lookup"}}],
        [{"type": "function"}],
        [{"type": "function", "function": {}}],
        [tool_definition(name=" ")],
        [tool_definition(description=None)],
        [tool_definition(parameters=[])],
        [tool_definition(parameters={"bad": float("nan")})],
        [tool_definition(parameters={"bad": float("inf")})],
        [tool_definition(parameters={"bad": object()})],
        [tool_definition(parameters={1: "not-a-string-key"})],
        [tool_definition(parameters={"enum": (1, 2)})],
        [tool_definition(strict=True)],
        [{**tool_definition(), "extra": "unsupported"}],
    ],
)
def test_invalid_tools_are_rejected(transport, tools):
    with pytest.raises(ModelInputError):
        transport.convert_tools(tools)


def test_schema_circular_reference_is_rejected(transport):
    schema = {"type": "object"}
    schema["properties"] = schema
    with pytest.raises(ModelInputError, match="circular"):
        transport.convert_tools([tool_definition(parameters=schema)])


def test_shared_schema_subobjects_are_copied_independently(transport):
    common = {"enum": [None, True, 2, 0.5, "text"]}
    result = transport.convert_tools([tool_definition(parameters={"a": common, "b": common})])
    parameters = result[0]["function"]["parameters"]
    assert parameters == {"a": common, "b": common}
    assert parameters["a"] is not parameters["b"]
    assert parameters["a"] is not common


@pytest.mark.parametrize("model", ["", " ", None, 2])
def test_invalid_model_is_rejected_at_transport_boundary(transport, model):
    with pytest.raises(ModelInputError):
        transport.build_kwargs(model, [{"role": "user", "content": "hello"}])


@pytest.mark.parametrize("sdk_response", [False, True])
def test_text_response_from_mapping_and_sdk(transport, sdk_response):
    payload = response()
    value = ChatCompletion.model_validate(payload) if sdk_response else payload
    result = transport.normalize_response(value)
    assert result.content == "hello"
    assert result.finish_reason == "stop"
    assert result.tool_calls == ()
    assert result.refusal is None
    assert result.usage is None


@pytest.mark.parametrize("count", [1, 3])
def test_pure_tool_calls_preserve_order_ids_and_raw_arguments(transport, count):
    calls = [
        wire_call(identifier=f"call_{index}", name=f"lookup_{index}", arguments="{unfinished ")
        for index in range(count)
    ]
    payload = response(content=None, finish_reason="tool_calls", tool_calls=calls)
    original = deepcopy(payload)
    result = transport.normalize_response(payload)
    assert result.content is None
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == tuple(
        ToolCall(f"call_{index}", f"lookup_{index}", "{unfinished ") for index in range(count)
    )
    assert payload == original


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (None, None),
        ({}, Usage()),
        ({"prompt_tokens": 3}, Usage(prompt_tokens=3)),
        ({"prompt_tokens": None, "completion_tokens": 0}, Usage(completion_tokens=0)),
        ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, Usage(0, 0, 0)),
        ({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}, Usage(2, 1, 3)),
    ],
)
def test_missing_partial_and_zero_usage_are_distinguished(transport, usage, expected):
    assert transport.normalize_response({**response(), "usage": usage}).usage == expected


def test_refusal_is_separate_from_content_and_keeps_actual_reason(transport):
    result = transport.normalize_response(response(content=None, refusal="Cannot comply."))
    assert result.content is None
    assert result.refusal == "Cannot comply."
    assert result.finish_reason == "stop"


@pytest.mark.parametrize("reason", ["length", "content_filter"])
@pytest.mark.parametrize("content", [None, "", " ", "partial answer"])
def test_truncation_and_filtering_keep_empty_or_partial_payload(transport, reason, content):
    result = transport.normalize_response(response(content=content, finish_reason=reason))
    assert result.content == content
    assert result.finish_reason == reason


def test_truncated_tool_payload_is_not_relabelled(transport):
    result = transport.normalize_response(
        response(content=None, finish_reason="length", tool_calls=[wire_call(arguments="{partial")])
    )
    assert result.finish_reason == "length"
    assert result.tool_calls[0].arguments == "{partial"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"choices": []},
        {"choices": response()["choices"] * 2},
        {"choices": response()["choices"][0]},
        {"choices": [None]},
        {"choices": [{}]},
        {"choices": [{"message": None, "finish_reason": "stop"}]},
        {"choices": [{"message": {}, "finish_reason": "stop"}]},
        response(role="user"),
        response(finish_reason=None),
        response(finish_reason=""),
        response(finish_reason=" "),
        response(finish_reason=3),
        response(content=[]),
        response(refusal=3),
        response(content=None),
        response(content=""),
        response(content=" "),
        response(content=None, refusal=" "),
        response(tool_calls={}),
        response(tool_calls=[None]),
        response(tool_calls=[{"id": "call_1", "type": "function"}]),
        response(tool_calls=[{**wire_call(), "type": "custom"}]),
        response(tool_calls=[wire_call(identifier=None)]),
        response(tool_calls=[wire_call(identifier=" ")]),
        response(tool_calls=[wire_call(name="")]),
        response(tool_calls=[wire_call(arguments={})]),
        response(tool_calls=[wire_call(), wire_call(name=None)]),
    ],
)
def test_malformed_response_is_rejected(transport, payload):
    with pytest.raises(ModelResponseError):
        transport.normalize_response(payload)


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens"])
@pytest.mark.parametrize("value", [-1, True, False, "1", 0.0, [], {}])
def test_malformed_usage_count_is_rejected(transport, field, value):
    with pytest.raises(ModelResponseError):
        transport.normalize_response({**response(), "usage": {field: value}})


@pytest.mark.parametrize("value", [[], "unknown", 0])
def test_malformed_usage_container_is_rejected(transport, value):
    with pytest.raises(ModelResponseError):
        transport.normalize_response({**response(), "usage": value})


def test_unknown_nonempty_finish_reason_is_not_changed(transport):
    result = transport.normalize_response(response(finish_reason="provider_new_reason"))
    assert result.finish_reason == "provider_new_reason"


def test_permissive_sdk_objects_do_not_bypass_validation(transport):
    payload = response(tool_calls=[wire_call(arguments={})])
    sdk_response = ChatCompletion.model_construct(**payload)
    with pytest.raises(ModelResponseError):
        transport.normalize_response(sdk_response)


def test_errors_do_not_echo_caller_or_provider_data(transport):
    secret = "confidential-user-content"
    with pytest.raises(ModelInputError) as input_error:
        transport.convert_messages([{"role": "user", "content": secret, secret: secret}])
    with pytest.raises(ModelResponseError) as response_error:
        transport.normalize_response(response(tool_calls=[wire_call(arguments={secret: secret})]))
    assert secret not in str(input_error.value)
    assert secret not in str(response_error.value)
