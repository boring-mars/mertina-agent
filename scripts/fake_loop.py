"""Run agent turns against a fake model: the v0.1 acceptance check.

A real ``AIAgent`` talks to a scripted chat-completions client instead of the network. v0.1 has no
built-in tool yet (the first one is ``web_search`` in v0.1.2), so this script registers its own
``clock`` tool in the real registry. Two scenarios:

- **Round trip.** The model asks for ``clock`` and builds its second reply from the tool result it
  was sent back: request, tool call, dispatch, tool result, final answer. The answer streams to
  ``stream_delta_callback`` and the call reports to ``tool_progress_callback``.
- **Stop and continue.** The model asks for two clock calls and the user stops the turn as the
  first one starts. Every tool call still gets a result, so the next turn, sent on the same
  history, passes the fake model's check that each tool call was answered.

Usage::

    uv run python -m scripts.fake_loop

Exit status 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from mertina.run_agent import AIAgent
from mertina.tools.registry import registry, tool_result

CLOCK_SCHEMA: dict[str, Any] = {
    "name": "clock",
    "description": "Get the current local date and time.",
    "parameters": {"type": "object", "properties": {}},
}


def register_clock() -> None:
    """Register this script's own tool. It is a fixture, not a built-in tool."""
    if registry.get_entry("clock") is None:
        registry.register(
            name="clock",
            toolset="demo",
            schema=CLOCK_SCHEMA,
            handler=lambda args, **kw: tool_result(weekday=datetime.now().strftime("%A")),
        )


def _message(content: str | None, tool_calls: list[Any] | None = None) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")
        ],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5, total_tokens=25),
    )


def stream_chunks(response: SimpleNamespace) -> Iterator[SimpleNamespace]:
    """``response`` as the chunks a ``stream=True`` request yields: text a word at a time, each
    tool call's name then its arguments, the finish reason, then a choiceless usage chunk."""
    choice = response.choices[0]
    message = choice.message

    def chunk(**delta: Any) -> SimpleNamespace:
        fields = {"content": None, "tool_calls": None, "reasoning_content": None, **delta}
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(**fields), finish_reason=None)]
        )

    if getattr(message, "reasoning_content", None):
        yield chunk(reasoning_content=message.reasoning_content)
    for i, word in enumerate(message.content.split(" ") if message.content else []):
        yield chunk(content=word if i == 0 else " " + word)
    for index, call in enumerate(message.tool_calls or []):
        named = SimpleNamespace(name=call.function.name, arguments=None)
        yield chunk(tool_calls=[SimpleNamespace(index=index, id=call.id, function=named)])
        args = SimpleNamespace(name=None, arguments=call.function.arguments)
        yield chunk(tool_calls=[SimpleNamespace(index=index, id=None, function=args)])
    yield SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(), finish_reason=choice.finish_reason)]
    )
    if response.usage is not None:
        yield SimpleNamespace(choices=[], usage=response.usage)


class FakeCompletions:
    """``client.chat.completions``: asks for ``clock`` (``calls`` times in one reply), then answers
    from its result. Like a real endpoint, it rejects a history with an unanswered tool call."""

    def __init__(self, calls: int = 1) -> None:
        self.calls = calls
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        messages = kwargs["messages"]
        answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
        for message in messages:
            for call in message.get("tool_calls") or []:
                if call["id"] not in answered:
                    raise ValueError(f"tool call {call['id']} has no tool result")
        response = self._reply(messages)
        return stream_chunks(response) if kwargs.get("stream") else response

    def _reply(self, messages: list[dict[str, Any]]) -> SimpleNamespace:
        weekdays = []
        for row in (m for m in messages if m.get("role") == "tool"):
            try:
                weekdays.append(json.loads(row["content"])["weekday"])
            except (ValueError, KeyError, TypeError):
                pass  # a call the stop skipped
        if not weekdays:
            calls = [
                SimpleNamespace(
                    id=f"call_clock_{n}",
                    type="function",
                    function=SimpleNamespace(name="clock", arguments="{}"),
                )
                for n in range(1, self.calls + 1)
            ]
            return _message(None, calls)
        return _message(f"Today is {weekdays[0]}.")


@dataclass
class Run:
    """One scenario's turns, the requests the fake model saw, and what the callbacks received."""

    results: list[dict[str, Any]]
    completions: FakeCompletions
    deltas: list[str | None] = field(default_factory=list)
    progress: list[tuple[str, str]] = field(default_factory=list)


def _agent(scenario: Run, on_progress: Callable[[str], None] | None = None) -> Any:
    """A real AIAgent on the scenario's fake client, recording what its callbacks receive."""

    def progress(event: str, name: str, *args: Any, **kwargs: Any) -> None:
        scenario.progress.append((event, name))
        if on_progress is not None:
            on_progress(event)

    agent: Any = AIAgent(
        base_url="http://fake-model.invalid/v1",
        api_key="not-a-real-key",
        model="fake-model",
        max_iterations=5,
        quiet_mode=True,
        stream_delta_callback=scenario.deltas.append,
        tool_progress_callback=progress,
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=scenario.completions))
    agent.client = client
    # Requests run on per-request clients built from the same factory as the shared one.
    agent._create_openai_client = lambda *args, **kwargs: client
    return agent


def run() -> Run:
    """One turn with a tool round trip."""
    register_clock()
    scenario = Run([], FakeCompletions())
    scenario.results.append(_agent(scenario).run_conversation("What day is it today?"))
    return scenario


def run_stop() -> Run:
    """A turn stopped as its first tool starts, then a turn on the same history."""
    register_clock()
    scenario = Run([], FakeCompletions(calls=2))
    stopped: list[bool] = []

    def stop_on_first_start(event: str) -> None:
        if event == "tool.started" and not stopped:
            stopped.append(True)
            agent.interrupt()

    agent = _agent(scenario, stop_on_first_start)
    first = agent.run_conversation("What day is it today?")
    after = agent.run_conversation("Go on.", conversation_history=first["messages"])
    scenario.results.extend([first, after])
    return scenario


def check(scenario: Run) -> list[str]:
    """Problems with the round trip; empty when it is complete."""
    problems = []
    (result,) = scenario.results
    requests = scenario.completions.requests
    roles = [m["role"] for m in result["messages"]]
    if roles != ["user", "assistant", "tool", "assistant"]:
        problems.append(f"unexpected transcript roles: {roles}")
    if len(requests) != 2:
        problems.append(f"expected 2 model requests, got {len(requests)}")
    elif [t["function"]["name"] for t in requests[0].get("tools", [])] != ["clock"]:
        problems.append("the first request did not offer the clock tool")
    if not result["completed"]:
        problems.append(f"turn did not complete: {result['turn_exit_reason']}")
    tool_rows = [m for m in result["messages"] if m["role"] == "tool"]
    if tool_rows:
        weekday = json.loads(tool_rows[0]["content"]).get("weekday")
        if result["final_response"] != f"Today is {weekday}.":
            problems.append(
                f"final answer does not use the tool result: {result['final_response']!r}"
            )
    text = [d for d in scenario.deltas if d is not None]
    if "".join(text).strip() != result["final_response"] or len(text) < 3:
        problems.append(f"the answer did not stream in pieces: {scenario.deltas!r}")
    if scenario.progress != [("tool.started", "clock"), ("tool.completed", "clock")]:
        problems.append(f"unexpected tool progress: {scenario.progress}")
    return problems


def check_stop(scenario: Run) -> list[str]:
    """Problems with the stop; empty when the stopped turn left a history the next turn can use."""
    problems = []
    stopped, after = scenario.results
    if not stopped["interrupted"]:
        problems.append(f"the first turn was not interrupted: {stopped['turn_exit_reason']}")
    calls = [c["id"] for m in stopped["messages"] for c in m.get("tool_calls") or []]
    answered = [m["tool_call_id"] for m in stopped["messages"] if m["role"] == "tool"]
    if calls != ["call_clock_1", "call_clock_2"] or sorted(answered) != calls:
        problems.append(f"tool calls {calls} were answered by {answered}")
    if not after["completed"] or not str(after["final_response"]).startswith("Today is "):
        problems.append(f"the turn after the stop failed: {after['final_response']!r}")
    return problems


def _print(label: str, scenario: Run) -> None:
    print(f"--- {label}")
    shown = 0
    for result in scenario.results:  # each turn's messages include the history before it
        for message in result["messages"][shown:]:
            detail = message.get("content") or json.dumps(
                [c["function"]["name"] for c in message.get("tool_calls", [])]
            )
            print(f"{message['role']:>9}: {detail}")
        shown = len(result["messages"])
    print(f"streamed: {scenario.deltas!r}")
    print(f"progress: {scenario.progress!r}")


def main() -> int:
    round_trip, stop = run(), run_stop()
    _print("round trip", round_trip)
    _print("stop and continue", stop)
    problems = check(round_trip) + check_stop(stop)
    for problem in problems:
        print(f"FAIL: {problem}")
    print(
        "OK: a streamed tool round trip, and a stop the next turn recovers from"
        if not problems
        else "FAILED"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
