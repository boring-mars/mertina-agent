"""The ddgs provider: result shapes, the disposable search worker, its deadline and interrupts.

The worker tests run the real child process through its upstream test hooks, so nothing reaches
DuckDuckGo.
"""

import json
import sys
import threading
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import mertina
from mertina.plugins.web.ddgs import provider as ddgs
from mertina.plugins.web.ddgs.provider import DDGSWebSearchProvider
from mertina.tools.interrupt import set_interrupt


@pytest.fixture(autouse=True)
def no_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ddgs, "_test_hook", None)


class _FakeDDGS:
    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    def __enter__(self) -> "_FakeDDGS":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def text(self, query: str, max_results: int) -> Iterator[dict[str, str]]:
        yield {"title": "One", "href": "https://one.test", "body": "first"}
        yield {"title": "Two", "url": "https://two.test", "body": "second"}
        yield {"title": "Three", "href": "https://three.test", "body": "ignored past the limit"}


def test_identity_and_availability() -> None:
    provider = DDGSWebSearchProvider()

    assert provider.name == "ddgs"
    assert provider.display_name == "DuckDuckGo (ddgs)"
    assert provider.supports_search()
    assert provider.is_available()  # ddgs is a runtime dependency


def test_hits_are_normalized_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=_FakeDDGS))

    assert ddgs._run_ddgs_search("q", 2) == [
        {"title": "One", "url": "https://one.test", "description": "first", "position": 1},
        {"title": "Two", "url": "https://two.test", "description": "second", "position": 2},
    ]


def test_search_wraps_the_bounded_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int]] = []

    def fake_bounded(query: str, safe_limit: int) -> list[dict[str, Any]]:
        seen.append((query, safe_limit))
        return [{"title": "T", "url": "u", "description": "d", "position": 1}]

    monkeypatch.setattr(ddgs, "_run_ddgs_search_bounded", fake_bounded)

    result = DDGSWebSearchProvider().search("python", limit=0)

    assert seen == [("python", 1)]  # the limit is floored at one
    assert result == {
        "success": True,
        "data": {"web": [{"title": "T", "url": "u", "description": "d", "position": 1}]},
    }


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (TimeoutError("slow"), "DuckDuckGo search timed out after 30s"),
        (ddgs._SearchInterrupted("stop"), "DuckDuckGo search interrupted"),
        (RuntimeError("blocked"), "DuckDuckGo search failed: blocked"),
    ],
)
def test_search_failures_become_error_results(
    monkeypatch: pytest.MonkeyPatch, error: Exception, message: str
) -> None:
    def fail(query: str, safe_limit: int) -> list[dict[str, Any]]:
        raise error

    monkeypatch.setattr(ddgs, "_run_ddgs_search_bounded", fail)

    result = DDGSWebSearchProvider().search("q")

    assert result["success"] is False
    assert result["error"].startswith(message)


def test_without_the_package_search_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DDGSWebSearchProvider, "is_available", lambda self: False)

    assert DDGSWebSearchProvider().search("q") == {
        "success": False,
        "error": "ddgs package is not installed — run `pip install ddgs`",
    }


def test_the_worker_path_entry_is_the_directory_above_the_package() -> None:
    assert Path(ddgs._plugins_path_entry()) == Path(mertina.__file__).resolve().parent.parent


def test_the_worker_round_trips_an_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ddgs, "_test_hook", "empty")

    assert ddgs._run_ddgs_search_bounded("q", 3) == []
    assert ddgs._last_worker_proc is not None and ddgs._last_worker_proc.poll() is not None


def test_a_hung_worker_is_killed_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ddgs, "_test_hook", "sleep")
    monkeypatch.setattr(ddgs, "_SEARCH_TIMEOUT_SECS", 1)

    with pytest.raises(TimeoutError, match="timed out after 1s"):
        ddgs._run_ddgs_search_bounded("q", 3)

    assert ddgs._last_worker_proc is not None and ddgs._last_worker_proc.poll() is not None


def test_an_interrupt_stops_the_wait_and_kills_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ddgs, "_test_hook", "sleep")
    timer = threading.Timer(0.5, set_interrupt, args=(True, threading.get_ident()))
    timer.start()
    try:
        with pytest.raises(ddgs._SearchInterrupted):
            ddgs._run_ddgs_search_bounded("q", 3)
    finally:
        timer.cancel()
        set_interrupt(False)

    assert ddgs._last_worker_proc is not None and ddgs._last_worker_proc.poll() is not None


class _ExitedProc:
    def poll(self) -> int:
        return 1


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "exited without a result"),
        ("not json", "invalid JSON"),
        ("[1]", "invalid envelope"),
        (json.dumps({"ok": False, "error": "rate limited"}), "rate limited"),
        (json.dumps({"ok": True, "results": {"a": 1}}), "non-list results"),
    ],
)
def test_malformed_envelopes_raise(raw: str, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        ddgs._parse_envelope(raw, _ExitedProc())  # type: ignore[arg-type]
