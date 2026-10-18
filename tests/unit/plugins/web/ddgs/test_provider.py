"""ddgs provider isolation: a disposable worker process with a hard deadline, offline."""

import asyncio
import io
import json
import os
import sys
import textwrap
from typing import ClassVar

import pytest

from mertina_agent.agent.interrupt import InterruptSignal, bind_interrupt_signal
from mertina_agent.plugins.web.ddgs import _search_worker, provider
from mertina_agent.plugins.web.ddgs.provider import DDGSWebSearchProvider


@pytest.fixture
def worker(tmp_path):
    """Write a fake worker script and return the command that runs it."""

    def _make(body):
        script = tmp_path / "worker.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        return [sys.executable, str(script)]

    return _make


def search(command, *, timeout_s=10.0, query="python", limit=3):
    engine = DDGSWebSearchProvider(timeout_s=timeout_s, worker_command=command)
    return asyncio.run(engine.search(query, limit))


def test_results_from_the_worker_are_normalized(worker):
    command = worker(
        """
        import json, sys
        request = json.load(sys.stdin)
        rows = [{"title": request["query"], "url": "https://a.example", "description": "d"}]
        rows.append({"title": 7, "url": None, "description": "e"})
        json.dump({"ok": True, "results": rows}, sys.stdout)
        """
    )

    result = search(command)

    assert result == {
        "success": True,
        "data": {
            "web": [
                {"title": "python", "url": "https://a.example", "description": "d", "position": 1},
                {"title": "7", "url": "None", "description": "e", "position": 2},
            ]
        },
    }


def test_worker_receives_the_query_and_a_positive_limit(worker):
    command = worker(
        """
        import json, sys
        request = json.load(sys.stdin)
        row = {"title": json.dumps(request), "url": "", "description": ""}
        json.dump({"ok": True, "results": [row]}, sys.stdout)
        """
    )

    result = search(command, query="news", limit=0)

    assert json.loads(result["data"]["web"][0]["title"]) == {"query": "news", "safe_limit": 1}


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (
            'import json, sys\njson.dump({"ok": False, "error": "rate limited"}, sys.stdout)',
            "rate limited",
        ),
        ("print('not json')", "invalid JSON"),
        ("pass", "exited without a result"),
        ('import json, sys\njson.dump(["not", "an", "object"], sys.stdout)', "invalid envelope"),
        ('import json, sys\njson.dump({"ok": True, "results": ["x"]}, sys.stdout)', "malformed"),
    ],
    ids=["reported-error", "invalid-json", "no-output", "not-an-object", "malformed-rows"],
)
def test_worker_failures_become_readable_results(worker, body, fragment):
    result = search(worker(body))

    assert result["success"] is False
    assert result["error"].startswith("DuckDuckGo search failed:")
    assert fragment in result["error"]


def test_hung_worker_is_killed_at_the_deadline(worker):
    command = worker("import time\ntime.sleep(60)\n")

    result = search(command, timeout_s=0.3)

    assert result == {
        "success": False,
        "error": "DuckDuckGo search timed out after 0.3s — DuckDuckGo may be rate-limiting or "
        "slow. Try again later.",
    }


def test_stop_request_ends_the_search(worker):
    signal = InterruptSignal()
    signal.set()
    command = worker("import time\ntime.sleep(60)\n")

    with bind_interrupt_signal(signal):
        result = search(command)

    assert result == {"success": False, "error": "DuckDuckGo search interrupted"}


def test_worker_that_cannot_start_is_reported(tmp_path):
    result = search([str(tmp_path / "missing-binary")])

    assert result["success"] is False
    assert result["error"].startswith("DuckDuckGo search failed:")


def test_worker_does_not_inherit_model_credentials(worker, monkeypatch):
    monkeypatch.setenv("MERTINA_LLM_API_KEY", "secret-key")
    monkeypatch.setenv("OPENAI_API_KEY", "other-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    command = worker(
        """
        import json, os, sys
        names = ("MERTINA_LLM_API_KEY", "OPENAI_API_KEY", "HTTPS_PROXY")
        seen = ",".join(name for name in names if name in os.environ)
        row = {"title": seen, "url": "", "description": ""}
        json.dump({"ok": True, "results": [row]}, sys.stdout)
        """
    )

    assert search(command)["data"]["web"][0]["title"] == "HTTPS_PROXY"


def test_cancelled_search_reaps_the_worker(worker, tmp_path):
    marker = tmp_path / "pid"
    command = worker(
        f"""
        import os, time
        open({str(marker)!r}, "w").write(str(os.getpid()))
        time.sleep(60)
        """
    )
    engine = DDGSWebSearchProvider(timeout_s=30, worker_command=command)

    async def scenario():
        task = asyncio.create_task(engine.search("q"))
        while not marker.exists() or not marker.read_text():
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_missing_package_is_reported_without_spawning(monkeypatch, worker):
    monkeypatch.setattr(provider.importlib.util, "find_spec", lambda _name: None)

    result = search(worker("raise SystemExit(1)"))

    assert result == {"success": False, "error": "ddgs package is not installed"}


def test_provider_identifies_itself_and_detects_the_installed_package():
    engine = DDGSWebSearchProvider()

    assert engine.name == "ddgs"
    assert engine.is_available() is True


def test_default_worker_serves_the_bundled_module():
    assert DDGSWebSearchProvider()._command[1:] == [
        "-m",
        "mertina_agent.plugins.web.ddgs._search_worker",
    ]


class FakeDDGS:
    """Stand-in for the ddgs client at its network boundary."""

    hits: ClassVar[list[dict[str, str]]] = [
        {"title": "A", "href": "https://a.example", "body": "first"},
        {"title": "B", "url": "https://b.example", "body": "second"},
        {"title": "C", "href": "https://c.example", "body": "third"},
    ]

    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def text(self, query, max_results):  # noqa: ARG002  mirrors ddgs.DDGS.text
        return iter(self.hits)


def test_ddgs_hits_are_normalized_and_capped(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)

    hits = provider._run_ddgs_search("q", 2)

    assert hits == [
        {"title": "A", "url": "https://a.example", "description": "first", "position": 1},
        {"title": "B", "url": "https://b.example", "description": "second", "position": 2},
    ]


def run_worker(monkeypatch, capsys, stdin_text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    code = _search_worker.main()
    return code, json.loads(capsys.readouterr().out)


def test_worker_main_writes_a_result_envelope(monkeypatch, capsys):
    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)

    code, envelope = run_worker(monkeypatch, capsys, '{"query": "q", "safe_limit": 1}')

    assert code == 0
    assert envelope["ok"] is True
    assert envelope["results"][0]["title"] == "A"


def test_worker_main_rejects_a_malformed_request(monkeypatch, capsys):
    code, envelope = run_worker(monkeypatch, capsys, "not json")

    assert code == 2
    assert envelope["ok"] is False
    assert envelope["error"].startswith("invalid request:")


def test_worker_main_reports_search_errors(monkeypatch, capsys):
    class BrokenDDGS(FakeDDGS):
        def text(self, query, max_results):  # noqa: ARG002  mirrors ddgs.DDGS.text
            message = "blocked"
            raise RuntimeError(message)

    monkeypatch.setattr("ddgs.DDGS", BrokenDDGS)

    code, envelope = run_worker(monkeypatch, capsys, '{"query": "q", "safe_limit": 1}')

    assert code == 1
    assert envelope == {"ok": False, "error": "RuntimeError: blocked"}


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM handling is POSIX-specific")
def test_worker_ignoring_terminate_is_killed(worker, monkeypatch):
    monkeypatch.setattr(provider, "_TERMINATE_GRACE_S", 0.2)
    command = worker(
        """
        import signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
        """
    )

    result = search(command, timeout_s=0.5)

    assert "timed out" in result["error"]


class GoneProcess:
    """A process handle whose process vanished before it could be signalled."""

    returncode = None
    pid = 1

    def terminate(self):
        raise ProcessLookupError

    def kill(self):
        raise ProcessLookupError


class UnkillableProcess:
    """A process handle that never reports exit, as a zombie outside our control would."""

    returncode = None
    pid = 2

    def terminate(self):
        return None

    def kill(self):
        return None

    async def wait(self):
        await asyncio.sleep(3600)


def test_reaping_a_process_that_already_exited_is_quiet():
    asyncio.run(provider._terminate_and_reap(GoneProcess()))


def test_unkillable_worker_is_logged_rather_than_awaited_forever(monkeypatch, caplog):
    monkeypatch.setattr(provider, "_TERMINATE_GRACE_S", 0.01)

    asyncio.run(provider._terminate_and_reap(UnkillableProcess()))

    assert "did not exit after kill" in caplog.text
