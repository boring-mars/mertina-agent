"""Outer-loop error handling on crafted histories."""

import pytest

from mertina_agent.agent.turn_loop_errors import handle_outer_loop_error


def assistant_with_calls(*ids):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": ident, "type": "function", "function": {"name": "echo", "arguments": "{}"}}
            for ident in ids
        ],
    }


def test_only_unanswered_calls_of_the_newest_batch_get_error_results():
    messages = [
        {"role": "user", "content": "go"},
        assistant_with_calls("a", "b"),
        {"role": "tool", "content": "done", "tool_call_id": "a"},
    ]

    verdict = handle_outer_loop_error(RuntimeError("boom"), messages=messages, api_call_count=1)

    assert verdict.action == "break"
    assert [message.get("tool_call_id") for message in messages[2:]] == ["a", "b"]
    assert messages[3]["content"].startswith("Error executing tool: ")
    assert messages[3]["content"].endswith("boom")


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "user", "content": "go"}],
        [assistant_with_calls("a"), {"role": "tool", "content": "x", "tool_call_id": "a"}],
    ],
    ids=["empty", "user-tail", "all-answered"],
)
def test_histories_without_open_calls_are_left_unchanged(messages):
    before = [dict(message) for message in messages]

    handle_outer_loop_error(ValueError("bad"), messages=messages, api_call_count=1)

    assert messages == before
