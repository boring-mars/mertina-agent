"""Stopping a turn at each checkpoint and continuing afterwards (Hermes interrupt intent)."""

import asyncio
import threading

import pytest

from mertina_agent.agent.core import Agent
from mertina_agent.agent.events import RunStopped
from mertina_agent.agent.tool_executor import INTERRUPTED_TOOL_RESULT
from mertina_agent.agent.turn_api_call import INTERRUPT_WAITING_FOR_MODEL_PREFIX, perform_api_call
from mertina_agent.agent.turn_failure_copy import FAILED_TURN_NOTICE, LOCAL_PROCESSING_ERROR
from mertina_agent.agent.turn_loop_errors import short_detail
from mertina_agent.tools.registry import ToolRegistry


class HangingClient:
    """A model endpoint that never answers, recording whether it was cancelled."""

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def complete(self, messages, *, tools=()):
        del messages, tools
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError  # pragma: no cover - never reached

    async def stream(self, messages, *, tools=(), on_text_delta=None, on_tool_started=None):
        del on_text_delta, on_tool_started
        return await self.complete(messages, tools=tools)

    async def aclose(self):
        return None


def test_stop_while_waiting_for_the_model_abandons_the_request(settings, tool_registry, fake):
    client = HangingClient()
    events = []
    agent = Agent(
        settings, model_client=client, tool_registry=tool_registry, event_callback=events.append
    )

    async def scenario():
        turn = asyncio.create_task(agent.run_conversation("Hi"))
        await client.started.wait()
        assert agent.interrupt("user_stop") is True
        return await asyncio.wait_for(turn, 5)

    result = asyncio.run(scenario())

    assert client.cancelled is True
    assert result["interrupted"] is True
    assert result["turn_exit_reason"] == "interrupted_during_api_call"
    assert result["final_response"].startswith(INTERRUPT_WAITING_FOR_MODEL_PREFIX)
    assert result["messages"][-1]["content"] == FAILED_TURN_NOTICE
    assert isinstance(events[-1], RunStopped)
    assert events[-1].turn_exit_reason == "interrupted_during_api_call"
    fake.assert_replayable(result["messages"])


def test_stop_from_another_thread_reaches_the_running_turn(settings, tool_registry):
    client = HangingClient()
    agent = Agent(settings, model_client=client, tool_registry=tool_registry)

    async def scenario():
        turn = asyncio.create_task(agent.run_conversation("Hi"))
        await client.started.wait()
        worker = threading.Thread(target=agent.interrupt)
        worker.start()
        worker.join()
        return await asyncio.wait_for(turn, 5)

    assert asyncio.run(scenario())["interrupted"] is True


def test_stop_between_tool_rounds_closes_the_history_and_allows_continuing(settings, fake):
    tool_registry = ToolRegistry()
    agent_holder = {}
    tool_registry.register(
        "stopper",
        "core",
        {"name": "stopper"},
        lambda _args: agent_holder["agent"].interrupt() and "ran",
    )
    client = fake.client(
        [
            fake.calls(fake.call("a", name="stopper"), fake.call("b", name="stopper")),
            fake.text("Resumed."),
        ]
    )
    agent = Agent(settings, model_client=client, tool_registry=tool_registry)
    agent_holder["agent"] = agent

    stopped = asyncio.run(agent.run_conversation("Do two things"))

    assert stopped["interrupted"] is True
    assert stopped["turn_exit_reason"] == "interrupted_by_user"
    assert [message["content"] for message in stopped["messages"][2:]] == [
        "ran",
        INTERRUPTED_TOOL_RESULT.format(name="stopper"),
        "Operation interrupted.",
    ]
    fake.assert_replayable(stopped["messages"])

    resumed = asyncio.run(
        agent.run_conversation("Continue", conversation_history=stopped["messages"])
    )

    assert resumed["completed"] is True
    assert resumed["final_response"] == "Resumed."
    assert client.requests[-1]["messages"][1:-1] == stopped["messages"]


def test_interrupt_without_a_running_turn_is_ignored(settings, tool_registry, fake):
    agent = Agent(
        settings, model_client=fake.client([fake.text("ok")]), tool_registry=tool_registry
    )

    assert agent.interrupt() is False
    assert agent.is_interrupted is False
    assert asyncio.run(agent.run_conversation("Hi"))["completed"] is True


def test_withdrawn_stop_lets_the_turn_continue(settings, fake):
    tool_registry = ToolRegistry()
    holder = {}

    def change_of_mind(_args):
        agent = holder["agent"]
        agent.interrupt()
        interrupted = agent.is_interrupted
        agent.clear_interrupt()
        return f"interrupted={interrupted}"

    tool_registry.register("change", "core", {"name": "change"}, change_of_mind)
    client = fake.client([fake.calls(fake.call(name="change")), fake.text("Finished.")])
    agent = Agent(settings, model_client=client, tool_registry=tool_registry)
    holder["agent"] = agent

    result = asyncio.run(agent.run_conversation("Go"))

    assert result["messages"][2]["content"] == "interrupted=True"
    assert result["completed"] is True


def test_perform_api_call_without_a_run_signal_simply_awaits(fake):
    client = fake.client([fake.text("direct")])

    verdict = asyncio.run(perform_api_call(client, [{"role": "user", "content": "x"}], ()))

    assert (verdict.action, verdict.response.content) == ("fallthrough", "direct")


class BrokenRegistry(ToolRegistry):
    """Simulates a local bug escaping the tool layer."""

    async def dispatch(self, name, args):
        del name, args
        message = "unexpected internal state"
        raise RuntimeError(message)


def test_local_bug_ends_the_turn_with_a_valid_history(settings, fake):
    tool_registry = BrokenRegistry()
    tool_registry.register("echo", "core", {"name": "echo"}, lambda _args: "never")
    agent = Agent(
        settings,
        model_client=fake.client([fake.calls(fake.call("c1"), fake.call("c2"))]),
        tool_registry=tool_registry,
    )

    result = asyncio.run(agent.run_conversation("Go"))

    assert result["failed"] is True
    assert result["turn_exit_reason"] == "local_processing_error(RuntimeError)"
    assert result["final_response"] == LOCAL_PROCESSING_ERROR.format(
        detail="unexpected internal state"
    )
    tool_results = [message for message in result["messages"] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_results] == ["c1", "c2"]
    fake.assert_replayable(result["messages"])


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("first line\nsecond line"), "first line"),
        (RuntimeError(""), "RuntimeError"),
        (RuntimeError("x" * 300), "x" * 199 + "…"),
    ],
)
def test_short_detail_keeps_the_first_line_within_bounds(error, expected):
    assert short_detail(error) == expected


class PartlyStreamingClient(HangingClient):
    """Streams some text, then hangs until it is cancelled."""

    async def stream(self, messages, *, tools=(), on_text_delta=None, on_tool_started=None):
        del on_tool_started
        on_text_delta("The answer is ")
        on_text_delta("forty")
        return await self.complete(messages, tools=tools)


def test_stop_mid_stream_keeps_the_text_already_streamed(settings, tool_registry, fake):
    client = PartlyStreamingClient()
    events = []
    agent = Agent(
        settings, model_client=client, tool_registry=tool_registry, event_callback=events.append
    )

    async def scenario():
        turn = asyncio.create_task(agent.run_conversation("Hi"))
        await client.started.wait()
        agent.interrupt()
        return await asyncio.wait_for(turn, 5)

    result = asyncio.run(scenario())

    assert result["final_response"] == "The answer is forty"
    assert result["messages"][-1] == {"role": "assistant", "content": "The answer is forty"}
    assert [event.text for event in events if hasattr(event, "text")] == ["The answer is ", "forty"]
    assert isinstance(events[-1], RunStopped)
    fake.assert_replayable(result["messages"])
