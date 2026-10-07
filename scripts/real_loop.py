"""Run agent turns against a real model endpoint: the v0.1.2 acceptance check.

The endpoint comes from where ``run_agent.main()`` reads it: the Mertina home ``.env`` is loaded,
then config.yaml ``model`` (a model name, or ``default`` / ``base_url`` / ``api_key``), then
``OPENAI_BASE_URL`` / ``OPENAI_API_KEY``. The only tool offered is the built-in ``web_search``,
served by the bundled ddgs provider, so this script talks to the model endpoint and to DuckDuckGo.
Two scenarios:

- **Search and answer.** The model is asked a question it can only answer from a search. It calls
  ``web_search``, gets results back, and answers from them. The answer streams to
  ``stream_delta_callback`` and the call reports to ``tool_progress_callback``.
- **Stop and continue.** The same question, stopped as the first tool call starts. Every tool call
  still gets a result, so the next turn, sent on the same history, is accepted by the real API.

Usage::

    uv run python -m scripts.real_loop

Exit status 0 when every check passes, 1 when a check fails, 2 when no endpoint is configured.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from mertina.cli.env_loader import load_mertina_dotenv
from mertina.run_agent import AIAgent, _endpoint_from_config

QUESTION = (
    "Use the web_search tool to find the latest stable release of Python, "
    "then answer in one sentence with its version number."
)
FOLLOW_UP = "Go on, and answer the question."
# Header line, a blank line, the tool's output, the closing tag.
_UNTRUSTED_BLOCK = re.compile(
    r'<untrusted_tool_result source="[^"]*">\n.*?\n\n(.*)\n</untrusted_tool_result>\Z', re.DOTALL
)


@dataclass
class Endpoint:
    model: str
    base_url: str
    api_key: str | None


@dataclass
class Run:
    """One scenario's turns and what the callbacks received."""

    results: list[dict[str, Any]] = field(default_factory=list)
    deltas: list[str | None] = field(default_factory=list)
    progress: list[tuple[str, str]] = field(default_factory=list)


def endpoint() -> Endpoint | None:
    """The configured endpoint, or None when the model name or base URL is missing."""
    load_mertina_dotenv()
    model, base_url, api_key = _endpoint_from_config("", "", None)
    return Endpoint(model, base_url, api_key) if model and base_url else None


def _agent(target: Endpoint, scenario: Run, stop_on_first_tool: bool = False) -> Any:
    """A real AIAgent on the configured endpoint, recording what its callbacks receive."""
    agent: Any = None

    def progress(event: str, name: str, *args: Any, **kwargs: Any) -> None:
        if stop_on_first_tool and event == "tool.started" and not scenario.progress:
            agent.interrupt()
        scenario.progress.append((event, name))

    agent = AIAgent(
        base_url=target.base_url,
        api_key=target.api_key,
        model=target.model,
        max_iterations=6,
        quiet_mode=True,
        stream_delta_callback=scenario.deltas.append,
        tool_progress_callback=progress,
    )
    return agent


def run(target: Endpoint) -> Run:
    """One turn that has to search before it can answer."""
    scenario = Run()
    scenario.results.append(_agent(target, scenario).run_conversation(QUESTION))
    return scenario


def run_stop(target: Endpoint) -> Run:
    """A turn stopped as its first tool starts, then a turn on the same history."""
    scenario = Run()
    agent = _agent(target, scenario, stop_on_first_tool=True)
    first = agent.run_conversation(QUESTION)
    after = agent.run_conversation(FOLLOW_UP, conversation_history=first["messages"])
    scenario.results.extend([first, after])
    return scenario


def _unwrap(content: str) -> str:
    """The tool's own output inside the ``<untrusted_tool_result>`` block the agent wraps
    ``web_search`` results in (``agent/tool_dispatch_helpers._maybe_wrap_untrusted``)."""
    match = _UNTRUSTED_BLOCK.match(content)
    return match.group(1) if match else content


def _search_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The decoded ``web_search`` results in a turn's messages."""
    names = {c["id"]: c["function"]["name"] for m in messages for c in m.get("tool_calls") or []}
    out = []
    for message in messages:
        if message["role"] == "tool" and names.get(message.get("tool_call_id")) == "web_search":
            try:
                out.append(json.loads(_unwrap(message["content"])))
            except (TypeError, ValueError):
                out.append({"success": False, "error": str(message["content"])})
    return out


def check(scenario: Run) -> list[str]:
    """Problems with the search round trip; empty when it is complete."""
    (result,) = scenario.results
    if result.get("failed"):
        return [f"the model request failed: {result.get('error')}"]
    problems = []
    if not result.get("completed"):
        problems.append(f"turn did not complete: {result.get('turn_exit_reason')}")
    searches = _search_results(result["messages"])
    if not searches:
        problems.append("the model did not call web_search")
    elif not any(s.get("success") and s.get("data", {}).get("web") for s in searches):
        problems.append(f"web_search returned no results: {searches!r}")
    answer = str(result.get("final_response") or "").strip()
    if not answer:
        problems.append("the turn ended without an answer")
    text = [d for d in scenario.deltas if d is not None]
    if "".join(text).strip() != answer:
        problems.append("the streamed text does not add up to the final answer")
    if ("tool.started", "web_search") not in scenario.progress:
        problems.append(f"tool progress did not report web_search: {scenario.progress}")
    return problems


def check_stop(scenario: Run) -> list[str]:
    """Problems with the stop; empty when the real API accepted the history after it."""
    stopped, after = scenario.results
    failed = [r for r in (stopped, after) if r.get("failed")]
    if failed:
        return [f"the model request failed: {failed[0].get('error')}"]
    problems = []
    if not stopped.get("interrupted"):
        problems.append(f"the first turn was not interrupted: {stopped.get('turn_exit_reason')}")
    calls = [c["id"] for m in stopped["messages"] for c in m.get("tool_calls") or []]
    answered = [m["tool_call_id"] for m in stopped["messages"] if m["role"] == "tool"]
    if not calls or sorted(answered) != sorted(calls):
        problems.append(f"tool calls {calls} were answered by {answered}")
    if not after.get("completed") or not str(after.get("final_response") or "").strip():
        problems.append(f"the turn after the stop failed: {after.get('final_response')!r}")
    return problems


def _print(label: str, scenario: Run) -> None:
    print(f"--- {label}")
    shown = 0
    for result in scenario.results:  # each turn's messages include the history before it
        for message in result["messages"][shown:]:
            detail = message.get("content") or json.dumps(
                [c["function"]["name"] for c in message.get("tool_calls", [])]
            )
            print(f"{message['role']:>9}: {str(detail)[:300]}")
        shown = len(result["messages"])
    print(f"progress: {scenario.progress!r}")


def main() -> int:
    target = endpoint()
    if target is None:
        print(
            "No model endpoint configured. Set model (and model.base_url) in config.yaml under the "
            "Mertina home, or OPENAI_BASE_URL and OPENAI_API_KEY in its .env."
        )
        return 2
    print(f"endpoint: {target.base_url} model: {target.model}")
    search, stop = run(target), run_stop(target)
    _print("search and answer", search)
    _print("stop and continue", stop)
    problems = check(search) + check_stop(stop)
    for problem in problems:
        print(f"FAIL: {problem}")
    print(
        "OK: a streamed web_search round trip, and a stop the next turn recovers from"
        if not problems
        else "FAILED"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
