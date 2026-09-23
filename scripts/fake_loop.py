"""Run one full agent turn against a fake model: the v0.1.0 acceptance check.

A real ``AIAgent`` talks to a scripted chat-completions client instead of the network. v0.1.0 has
no built-in tool yet (the first one is ``web_search`` in v0.1.2), so this script registers its own
``clock`` tool in the real registry. The model asks for it and builds its second reply from the
tool result it was sent back, so the script proves the whole round trip: request, tool call,
dispatch, tool result, final answer.

Usage::

    uv run python -m scripts.fake_loop

Exit status 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
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


class FakeCompletions:
    """``client.chat.completions``: asks for ``clock``, then answers from its result."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(kwargs)
        tool_rows = [m for m in kwargs["messages"] if m.get("role") == "tool"]
        if not tool_rows:
            call = SimpleNamespace(
                id="call_clock_1",
                type="function",
                function=SimpleNamespace(name="clock", arguments="{}"),
            )
            return _message(None, [call])
        weekday = json.loads(tool_rows[-1]["content"])["weekday"]
        return _message(f"Today is {weekday}.")


def run() -> tuple[dict[str, Any], FakeCompletions]:
    """One turn of a real AIAgent against the fake client."""
    register_clock()
    agent = AIAgent(
        base_url="http://fake-model.invalid/v1",
        api_key="not-a-real-key",
        model="fake-model",
        max_iterations=5,
        quiet_mode=True,
    )
    completions = FakeCompletions()
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return agent.run_conversation("What day is it today?"), completions


def check(result: dict[str, Any], completions: FakeCompletions) -> list[str]:
    """Problems with the turn; empty when the round trip is complete."""
    problems = []
    roles = [m["role"] for m in result["messages"]]
    if roles != ["user", "assistant", "tool", "assistant"]:
        problems.append(f"unexpected transcript roles: {roles}")
    if len(completions.requests) != 2:
        problems.append(f"expected 2 model requests, got {len(completions.requests)}")
    elif [t["function"]["name"] for t in completions.requests[0].get("tools", [])] != ["clock"]:
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
    return problems


def main() -> int:
    result, completions = run()
    for message in result["messages"]:
        detail = message.get("content") or json.dumps(
            [c["function"]["name"] for c in message.get("tool_calls", [])]
        )
        print(f"{message['role']:>9}: {detail}")
    print(f"api_calls={result['api_calls']} total_tokens={result['total_tokens']}")
    problems = check(result, completions)
    for problem in problems:
        print(f"FAIL: {problem}")
    print("OK: full loop with a tool round trip" if not problems else "FAILED")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
