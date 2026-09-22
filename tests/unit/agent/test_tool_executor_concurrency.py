"""Parallel and segmented tool execution (Hermes concurrent-batch intent)."""

import asyncio

import pytest

from mertina_agent.agent import tool_executor
from mertina_agent.agent.events import ToolCallFinished, ToolCallStarted
from mertina_agent.agent.interrupt import InterruptSignal, bind_interrupt_signal
from mertina_agent.agent.tool_executor import (
    INTERRUPTED_TOOL_RESULT,
    execute_tool_calls,
    make_tool_result_message,
)
from mertina_agent.agent.transports import ToolCall
from mertina_agent.tools.registry import ToolRegistry

# Synthetic notices pass through the same untrusted-content wrapping as search
# output, exactly as in Hermes.
SKIPPED_SEARCH = make_tool_result_message(
    "web_search", INTERRUPTED_TOOL_RESULT.format(name="web_search"), "id"
)["content"]


def call(identifier, name="web_search", arguments='{"query": "q"}'):
    return ToolCall(id=identifier, name=name, arguments=arguments)


def run(tool_calls, tool_registry, **kwargs):
    messages = []
    asyncio.run(execute_tool_calls(tool_calls, messages, tool_registry=tool_registry, **kwargs))
    return messages


@pytest.fixture
def log():
    return []


@pytest.fixture
def parallel_registry(log):
    """A parallel-safe tool whose calls each wait until two of them are running."""
    tool_registry = ToolRegistry()
    state = {"running": 0}

    async def search(args):
        state["running"] += 1
        log.append(("start", args["query"]))
        while state["running"] < 2:
            await asyncio.sleep(0)
        log.append(("end", args["query"]))
        return f"results for {args['query']}"

    tool_registry.register("web_search", "web", {"name": "web_search"}, search, is_async=True)
    tool_registry.register(
        "echo", "core", {"name": "echo"}, lambda _args: log.append(("echo", "")) or "echoed"
    )
    return tool_registry


def test_parallel_safe_calls_run_concurrently(parallel_registry, log):
    messages = run(
        [call("a", arguments='{"query": "one"}'), call("b", arguments='{"query": "two"}')],
        parallel_registry,
    )

    assert [entry[0] for entry in log[:2]] == ["start", "start"]
    assert [message["content"] for message in messages] == [
        "results for one",
        "results for two",
    ]


def test_results_keep_call_order_when_later_calls_finish_first():
    tool_registry = ToolRegistry()
    first_may_finish = asyncio.Event()

    async def search(args):
        if args["query"] == "slow":
            await first_may_finish.wait()
        else:
            first_may_finish.set()
        return args["query"]

    tool_registry.register("web_search", "web", {"name": "web_search"}, search, is_async=True)

    messages = run(
        [call("a", arguments='{"query": "slow"}'), call("b", arguments='{"query": "fast"}')],
        tool_registry,
    )

    assert [(message["tool_call_id"], message["content"]) for message in messages] == [
        ("a", "slow"),
        ("b", "fast"),
    ]


def test_sequential_tool_is_a_barrier_between_parallel_runs(parallel_registry, log):
    tool_calls = [
        call("a", arguments='{"query": "1"}'),
        call("b", arguments='{"query": "2"}'),
        call("c", name="echo", arguments="{}"),
        call("d", arguments='{"query": "3"}'),
        call("e", arguments='{"query": "4"}'),
    ]

    messages = run(tool_calls, parallel_registry)

    echo_at = log.index(("echo", ""))
    assert set(log[:echo_at]) >= {("end", "1"), ("end", "2")}
    assert ("start", "3") in log[echo_at:]
    assert [message["tool_call_id"] for message in messages] == ["a", "b", "c", "d", "e"]


def test_single_parallel_safe_call_between_barriers_runs_sequentially(log):
    tool_registry = ToolRegistry()
    tool_registry.register(
        "web_search", "web", {"name": "web_search"}, lambda _args: log.append("search") or "found"
    )
    tool_registry.register(
        "echo", "core", {"name": "echo"}, lambda _args: log.append("echo") or "x"
    )

    messages = run(
        [call("a", name="echo", arguments="{}"), call("b"), call("c", "echo", "{}")], tool_registry
    )

    assert log == ["echo", "search", "echo"]
    assert [message["tool_call_id"] for message in messages] == ["a", "b", "c"]


def test_invalid_arguments_break_the_parallel_run_in_place():
    tool_registry = ToolRegistry()
    tool_registry.register("web_search", "web", {"name": "web_search"}, lambda args: args["query"])
    tool_calls = [
        call("a", arguments='{"query": "1"}'),
        call("b", arguments="not json"),
        call("c", arguments='{"query": "2"}'),
    ]

    messages = run(tool_calls, tool_registry)

    assert [message["tool_call_id"] for message in messages] == ["a", "b", "c"]
    assert '"error": "Invalid tool arguments"' in messages[1]["content"]


def test_one_failing_call_does_not_affect_its_batch():
    tool_registry = ToolRegistry()

    async def search(args):
        if args["query"] == "bad":
            message = "backend down"
            raise RuntimeError(message)
        return "ok"

    tool_registry.register("web_search", "web", {"name": "web_search"}, search, is_async=True)
    events = []

    messages = run(
        [call("a", arguments='{"query": "bad"}'), call("b", arguments='{"query": "good"}')],
        tool_registry,
        event_callback=events.append,
    )

    assert "backend down" in messages[0]["content"]
    assert messages[1]["content"] == "ok"
    finished = {
        event.call_id: event.is_error for event in events if isinstance(event, ToolCallFinished)
    }
    assert finished == {"a": True, "b": False}
    assert sum(isinstance(event, ToolCallStarted) for event in events) == 2


def test_stop_before_a_parallel_batch_skips_every_call(parallel_registry, log):
    signal = InterruptSignal()
    signal.set()

    with bind_interrupt_signal(signal):
        messages = run([call("a"), call("b")], parallel_registry)

    assert log == []
    assert {message["content"] for message in messages} == {SKIPPED_SEARCH}


def test_stop_during_a_parallel_batch_abandons_calls_after_the_grace_period(monkeypatch):
    monkeypatch.setattr(tool_executor, "_INTERRUPT_GRACE_S", 0.01)
    monkeypatch.setattr(tool_executor, "_INTERRUPT_POLL_S", 0.01)
    signal = InterruptSignal()
    tool_registry = ToolRegistry()
    never = asyncio.Event()

    async def search(args):
        if args["query"] == "stop":
            signal.set()
            return "stopped the run"
        await never.wait()
        return "never returned"

    tool_registry.register("web_search", "web", {"name": "web_search"}, search, is_async=True)

    with bind_interrupt_signal(signal):
        messages = run(
            [call("a", arguments='{"query": "stop"}'), call("b", arguments='{"query": "hang"}')],
            tool_registry,
        )

    assert [message["content"] for message in messages] == ["stopped the run", SKIPPED_SEARCH]


def test_cancelling_the_turn_cancels_running_workers():
    tool_registry = ToolRegistry()
    cancelled = []
    never = asyncio.Event()

    async def search(args):
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.append(args["query"])
            raise
        return "never"

    tool_registry.register("web_search", "web", {"name": "web_search"}, search, is_async=True)

    async def scenario():
        batch = asyncio.create_task(
            execute_tool_calls(
                [call("a", arguments='{"query": "1"}'), call("b", arguments='{"query": "2"}')],
                [],
                tool_registry=tool_registry,
            )
        )
        await asyncio.sleep(0.05)
        batch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await batch
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert sorted(cancelled) == ["1", "2"]


def test_at_most_eight_calls_run_at_once():
    tool_registry = ToolRegistry()
    state = {"running": 0, "peak": 0}

    async def search(_args):
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        await asyncio.sleep(0.01)
        state["running"] -= 1
        return "ok"

    tool_registry.register("web_search", "web", {"name": "web_search"}, search, is_async=True)

    run([call(f"c{index}") for index in range(12)], tool_registry)

    assert state["peak"] == 8


def test_concurrent_batch_answers_invalid_arguments_without_running_them(log):
    tool_registry = ToolRegistry()
    tool_registry.register(
        "web_search", "web", {"name": "web_search"}, lambda args: log.append(args) or "ran"
    )
    messages = []

    asyncio.run(
        tool_executor.execute_tool_calls_concurrent(
            [call("a", arguments="[1]"), call("b")], messages, tool_registry=tool_registry
        )
    )

    assert log == [{"query": "q"}]
    assert '"error": "Invalid tool arguments"' in messages[0]["content"]
    assert messages[1]["content"] == "ran"


def test_calls_finishing_within_the_grace_period_keep_their_results(monkeypatch):
    monkeypatch.setattr(tool_executor, "_INTERRUPT_POLL_S", 0.01)
    monkeypatch.setattr(tool_executor, "_INTERRUPT_GRACE_S", 5.0)
    signal = InterruptSignal()
    tool_registry = ToolRegistry()

    async def search(args):
        if args["query"] == "stop":
            signal.set()
        await asyncio.sleep(0.05)
        return f"done {args['query']}"

    tool_registry.register("web_search", "web", {"name": "web_search"}, search, is_async=True)

    with bind_interrupt_signal(signal):
        messages = run(
            [call("a", arguments='{"query": "stop"}'), call("b", arguments='{"query": "other"}')],
            tool_registry,
        )

    assert [message["content"] for message in messages] == ["done stop", "done other"]
