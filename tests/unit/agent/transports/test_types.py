from dataclasses import FrozenInstanceError

import pytest

from mertina_agent.agent.transports import NormalizedResponse, ToolCall, Usage
from mertina_agent.exceptions import ModelResponseError


def test_tool_arguments_remain_opaque():
    call = ToolCall(id="call_1", name="lookup", arguments="{unfinished")
    assert call.arguments == "{unfinished"


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", None), ("id", ""), ("id", " "), ("name", ""), ("name", 7), ("arguments", {})],
)
def test_invalid_tool_fields_are_rejected(field, value):
    fields = {"id": "call_1", "name": "lookup", "arguments": "{}", field: value}
    with pytest.raises(ModelResponseError):
        ToolCall(**fields)


def test_usage_unknown_and_zero_are_distinct():
    assert Usage() == Usage(None, None, None)
    assert Usage(0, 0, 0) != Usage()
    assert Usage(prompt_tokens=2).total_tokens is None


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens"])
@pytest.mark.parametrize("value", [-1, 1.0, True, False, "1", [], {}])
def test_invalid_usage_counts_are_rejected(field, value):
    with pytest.raises(ModelResponseError):
        Usage(**{field: value})


@pytest.mark.parametrize(
    "result",
    [ToolCall("call_1", "lookup", "{}"), Usage(1, 2, 3), NormalizedResponse("hello", (), "stop")],
)
def test_results_are_frozen(result):
    field = next(iter(result.__dataclass_fields__))
    with pytest.raises(FrozenInstanceError):
        setattr(result, field, None)


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_incomplete_empty_results_keep_terminal_reason(reason):
    result = NormalizedResponse(None, (), reason)
    assert result.finish_reason == reason


@pytest.mark.parametrize(
    "fields",
    [
        {"finish_reason": ""},
        {"finish_reason": None},
        {"content": []},
        {"tool_calls": []},
        {"tool_calls": ("invalid",)},
        {"refusal": {}},
        {"usage": {}},
        {"content": None},
        {"content": "  "},
    ],
)
def test_invalid_normalized_result_is_rejected(fields):
    values = {"content": "hello", "tool_calls": (), "finish_reason": "stop", **fields}
    with pytest.raises(ModelResponseError):
        NormalizedResponse(**values)
