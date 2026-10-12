"""Loop exits driven by a stop request, exercised through the loop entry point."""

import asyncio

from mertina_agent.agent.conversation_loop import run_conversation
from mertina_agent.agent.interrupt import InterruptSignal, bind_interrupt_signal
from mertina_agent.agent.tool_executor import INTERRUPTED_TOOL_RESULT
from mertina_agent.agent.turn_context import build_api_messages
from mertina_agent.agent.turn_failure_copy import FAILED_TURN_NOTICE
from mertina_agent.tools.model_tools import get_tool_definitions


def run(client, tool_registry, signal):
    tools = get_tool_definitions(tool_registry=tool_registry)

    async def scenario():
        with bind_interrupt_signal(signal):
            return await run_conversation(
                "Hi",
                model_client=client,
                active_system_prompt="system",
                tools=tools,
                valid_tool_names={"echo", "stopper"},
                tool_registry=tool_registry,
                max_iterations=5,
                turn_id="turn-test",
            )

    return asyncio.run(scenario())


def test_stop_before_the_first_call_makes_no_request(tool_registry, fake):
    signal = InterruptSignal()
    signal.set()
    client = fake.client([])

    result = run(client, tool_registry, signal)

    assert client.requests == []
    assert result["interrupted"] is True
    assert result["completed"] is False
    assert result["api_calls"] == 0
    assert result["messages"][-1] == {"role": "assistant", "content": FAILED_TURN_NOTICE}


def test_stop_during_a_tool_round_closes_the_tool_tail(tool_registry, fake):
    signal = InterruptSignal()
    tool_registry.register(
        "stopper", "core", {"name": "stopper"}, lambda _args: signal.set() or "ran"
    )
    batch = fake.calls(fake.call("a", name="stopper"), fake.call("b", name="echo"))
    client = fake.client([batch])

    result = run(client, tool_registry, signal)

    assert len(client.requests) == 1
    assert result["interrupted"] is True
    assert result["turn_exit_reason"] == "interrupted_by_user"
    assert [message["content"] for message in result["messages"][2:]] == [
        "ran",
        INTERRUPTED_TOOL_RESULT.format(name="echo"),
        "Operation interrupted.",
    ]
    fake.assert_replayable(result["messages"])


def test_api_messages_without_a_system_prompt_are_a_plain_copy():
    history = [{"role": "user", "content": "Hi"}]

    api_messages = build_api_messages(history, active_system_prompt="")

    assert api_messages == history
    assert api_messages[0] is not history[0]
