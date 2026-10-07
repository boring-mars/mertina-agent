"""The v0.1.2 acceptance script's endpoint lookup and checks, without a network.

The script itself talks to a real endpoint and to DuckDuckGo; run it by hand with
``uv run python -m scripts.real_loop``.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mertina.agent.tool_dispatch_helpers import _maybe_wrap_untrusted
from scripts import real_loop
from scripts.real_loop import Run


@pytest.fixture(autouse=True)
def no_openai_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _call(call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "web_search", "arguments": "{}"},
    }


def _tool(call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A tool message as the agent stores it: long web_search output is wrapped as untrusted."""
    content = _maybe_wrap_untrusted("web_search", json.dumps(payload, indent=2))
    return {"role": "tool", "tool_call_id": call_id, "content": content}


_HIT = {"success": True, "data": {"web": [{"title": "Python 3.14", "url": "https://python.test"}]}}


def _search_run(payload: dict[str, Any] = _HIT, answer: str = "Python 3.14 is out.") -> Run:
    messages = [
        {"role": "user", "content": real_loop.QUESTION},
        {"role": "assistant", "content": None, "tool_calls": [_call("c1")]},
        _tool("c1", payload),
        {"role": "assistant", "content": answer},
    ]
    result = {"completed": True, "messages": messages, "final_response": answer}
    return Run(
        results=[result],
        deltas=[None, "Python 3.14", " is out."],
        progress=[("tool.started", "web_search"), ("tool.completed", "web_search")],
    )


def test_without_an_endpoint_main_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert real_loop.endpoint() is None
    assert real_loop.main() == 2
    assert "No model endpoint configured" in capsys.readouterr().out


def test_the_endpoint_comes_from_config_and_the_dotenv(mertina_home: Path) -> None:
    (mertina_home / "config.yaml").write_text(
        "model:\n  default: gpt-x\n  base_url: http://llm.test/v1\n", encoding="utf-8"
    )
    (mertina_home / ".env").write_text("OPENAI_API_KEY=sk-dotenv\n", encoding="utf-8")

    assert real_loop.endpoint() == real_loop.Endpoint("gpt-x", "http://llm.test/v1", "sk-dotenv")


def test_a_complete_search_round_trip_passes() -> None:
    assert real_loop.check(_search_run()) == []


def test_a_round_trip_without_a_search_fails() -> None:
    scenario = _search_run()
    scenario.results[0]["messages"] = scenario.results[0]["messages"][:1]
    scenario.progress = []

    problems = real_loop.check(scenario)

    assert "the model did not call web_search" in problems
    assert any(p.startswith("tool progress did not report web_search") for p in problems)


def test_an_empty_search_fails() -> None:
    problems = real_loop.check(_search_run({"success": True, "data": {"web": []}}))

    assert any(p.startswith("web_search returned no results") for p in problems)


def test_an_answer_that_did_not_stream_fails() -> None:
    scenario = _search_run()
    scenario.deltas = [None]

    assert real_loop.check(scenario) == ["the streamed text does not add up to the final answer"]


def _stop_run(answered: bool = True, after_answer: str = "It is 3.14.") -> Run:
    stopped_messages = [
        {"role": "user", "content": real_loop.QUESTION},
        {"role": "assistant", "content": None, "tool_calls": [_call("c1"), _call("c2")]},
        _tool("c1", {"error": "interrupted"}),
    ]
    if answered:
        stopped_messages.append(_tool("c2", {"error": "interrupted"}))
    stopped = {"interrupted": True, "completed": False, "messages": stopped_messages}
    after = {"completed": True, "messages": [], "final_response": after_answer}
    return Run(results=[stopped, after])


def test_a_stop_the_next_turn_recovers_from_passes() -> None:
    assert real_loop.check_stop(_stop_run()) == []


def test_an_unanswered_tool_call_fails() -> None:
    problems = real_loop.check_stop(_stop_run(answered=False))

    assert problems == ["tool calls ['c1', 'c2'] were answered by ['c1']"]


def test_an_empty_turn_after_the_stop_fails() -> None:
    problems = real_loop.check_stop(_stop_run(after_answer=""))

    assert problems == ["the turn after the stop failed: ''"]


def test_a_failed_request_is_reported_on_its_own() -> None:
    failed = {"completed": False, "failed": True, "error": "HTTP 403", "messages": []}

    assert real_loop.check(Run(results=[failed])) == ["the model request failed: HTTP 403"]
    assert real_loop.check_stop(Run(results=[failed, failed])) == [
        "the model request failed: HTTP 403"
    ]


def test_a_wrapped_result_is_unwrapped_and_a_bare_one_kept() -> None:
    wrapped = _tool("c1", _HIT)["content"]

    assert wrapped.startswith("<untrusted_tool_result")
    assert json.loads(real_loop._unwrap(wrapped)) == _HIT
    assert real_loop._unwrap('{"error": "Interrupted"}') == '{"error": "Interrupted"}'
