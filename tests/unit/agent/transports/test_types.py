"""The normalized response types shared by every transport."""

import json
from types import SimpleNamespace

from mertina.agent.transports.types import (
    ToolCall,
    Usage,
    build_tool_call,
    map_finish_reason,
)


def test_build_tool_call_serializes_dict_arguments() -> None:
    call = build_tool_call("call_1", "get_time", {"tz": "UTC"})

    assert call.id == "call_1"
    assert call.name == "get_time"
    assert json.loads(call.arguments) == {"tz": "UTC"}
    assert call.provider_data is None


def test_build_tool_call_keeps_string_arguments_as_is() -> None:
    call = build_tool_call(None, "get_time", '{"tz": "UTC"}')

    assert call.arguments == '{"tz": "UTC"}'


def test_build_tool_call_puts_extra_fields_in_provider_data() -> None:
    call = build_tool_call("call_1", "get_time", {}, extra_content={"k": "v"})

    assert call.provider_data == {"extra_content": {"k": "v"}}


def test_tool_call_exposes_the_openai_function_shape() -> None:
    call = ToolCall(id="call_1", name="get_time", arguments="{}")

    assert call.type == "function"
    assert call.function.name == "get_time"
    assert call.function.arguments == "{}"


def test_usage_from_openai_treats_missing_counts_as_zero() -> None:
    raw = SimpleNamespace(prompt_tokens=10, completion_tokens=None)

    usage = Usage.from_openai(raw)

    assert usage == Usage(prompt_tokens=10, completion_tokens=0, total_tokens=0)


def test_map_finish_reason_falls_back_to_stop() -> None:
    mapping = {"end_turn": "stop", "tool_use": "tool_calls"}

    assert map_finish_reason("tool_use", mapping) == "tool_calls"
    assert map_finish_reason("unknown", mapping) == "stop"
    assert map_finish_reason(None, mapping) == "stop"
