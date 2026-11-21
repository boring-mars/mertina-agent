"""Run the v0.2 end-to-end cases and history growth curve on each database backend.

For every backend this script resets the session tables, starts the
deterministic stub model and the instrumented gateway, drives eight cases and a
growth curve over HTTP (plus the CLI for two of them), inspects the database,
and writes everything under ``dev/e2e/results/<run>/<backend>/``. It then
compares the backends and writes ``docs/testing/v0.2-e2e/README.md``.

Progress is printed as it happens: one line per step and case, a live bar for
the growth curve, and a heartbeat line whenever nothing has been printed for
five seconds, so a long step is visibly still running.

    uv run python dev/e2e/run_cases.py
    uv run python dev/e2e/run_cases.py --backends sqlite --growth-turns 50

PostgreSQL and MySQL use ``MERTINA_TEST_POSTGRESQL_URL`` / ``MERTINA_TEST_MYSQL_URL``
when set, otherwise the local development containers from ``dev/databases``.
Their ``mertina_*`` tables are dropped and recreated: never point these at data
you want to keep.
"""

import argparse
import asyncio
import contextlib
import json
import os
import signal
import socket
import statistics
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp
import inspect_db
import report
import stub_model
from pydantic import SecretStr
from sqlalchemy import text

from mertina_agent.config import Settings
from mertina_agent.database.runtime import open_database
from mertina_agent.state.schema import Schema

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RESULTS = HERE / "results"
LABELS = {"sqlite": "SQLite", "postgresql": "PostgreSQL", "mysql": "MySQL"}
DEFAULT_URLS = {
    "postgresql": "postgresql+asyncpg://mertina:mertina_local_dev@127.0.0.1:55432/mertina_dev",
    "mysql": "mysql+asyncmy://mertina:mertina_local_dev@127.0.0.1:53306/mertina_dev",
}
URL_VARIABLES = {
    "postgresql": "MERTINA_TEST_POSTGRESQL_URL",
    "mysql": "MERTINA_TEST_MYSQL_URL",
}
TERMINALS = {"run.completed", "run.partial", "run.stopped", "run.failed"}
MILESTONES = (1, 10, 50, 100, 200, 500, 1000)
SLOW_TOTAL_S = stub_model.SLOW_CHUNKS * stub_model.SLOW_DELAY_S

type Record = dict[str, object]


# --------------------------------------------------------------------------- output


class Console:
    """Progress output: steps, case results, a live bar and a heartbeat."""

    def __init__(self) -> None:
        """Use colour and in-place updates only on a terminal."""
        self.tty = sys.stdout.isatty()
        self.last_output = time.monotonic()
        self.activity = "starting"
        self.activity_since = time.monotonic()
        self._bar_open = False

    def _paint(self, code: str, value: str) -> str:
        return f"\033[{code}m{value}\033[0m" if self.tty else value

    def ok(self, value: str) -> str:
        """Green text."""
        return self._paint("32", value)

    def bad(self, value: str) -> str:
        """Red text."""
        return self._paint("31", value)

    def dim(self, value: str) -> str:
        """Grey text."""
        return self._paint("2", value)

    def bold(self, value: str) -> str:
        """Bold text."""
        return self._paint("1", value)

    def line(self, value: str = "") -> None:
        """Print one line, closing an in-place bar first."""
        if self._bar_open:
            sys.stdout.write("\n")
            self._bar_open = False
        sys.stdout.write(value + "\n")
        sys.stdout.flush()
        self.last_output = time.monotonic()

    def doing(self, activity: str) -> None:
        """Name what is running, for the heartbeat."""
        self.activity = activity
        self.activity_since = time.monotonic()

    def bar(self, label: str, done: int, total: int, extra: str) -> None:
        """Draw a progress bar; on a pipe, print every tenth of the way."""
        width = 24
        filled = int(width * done / total) if total else width
        text = f"  {label} [{'█' * filled}{'░' * (width - filled)}] {done}/{total} · {extra}"
        if self.tty:
            sys.stdout.write("\r\033[2K" + text)
            sys.stdout.flush()
            self._bar_open = True
            self.last_output = time.monotonic()
        elif done == total or done % max(1, total // 10) == 0:
            self.line(text)

    async def heartbeat(self) -> None:
        """Every second, report the current activity if the screen has been quiet."""
        while True:
            await asyncio.sleep(1)
            quiet = time.monotonic() - self.last_output
            if quiet >= 5:
                running = time.monotonic() - self.activity_since
                self.line(self.dim(f"    … still running: {self.activity} ({running:.0f}s)"))


CONSOLE = Console()


@contextlib.asynccontextmanager
async def step(label: str) -> AsyncIterator[Record]:
    """Print ``label`` with its outcome and duration; the dict collects a note."""
    CONSOLE.doing(label)
    note: Record = {}
    start = time.perf_counter()
    try:
        yield note
    except Exception as exc:
        CONSOLE.line(f"  · {label:<28} {CONSOLE.bad('✗')} {type(exc).__name__}: {exc}")
        raise
    elapsed = time.perf_counter() - start
    detail = f"  {note['detail']}" if note.get("detail") else ""
    CONSOLE.line(f"  · {label:<28} {CONSOLE.ok('✓')} {elapsed:.2f}s{CONSOLE.dim(detail)}")


def fmt_ms(value: float | None) -> str:
    """Milliseconds with sensible precision."""
    if value is None:
        return "-"
    return f"{value:.2f}ms" if value < 10 else f"{value:.1f}ms"


def percentile(values: list[float], share: float) -> float | None:
    """Nearest-rank percentile, or None without data."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return ordered[index]


# --------------------------------------------------------------------------- processes


def free_port() -> int:
    """Ask the OS for an unused loopback port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def child_environment() -> dict[str, str]:
    """The current environment without MERTINA_* variables, so env files decide."""
    return {key: value for key, value in os.environ.items() if not key.startswith("MERTINA_")}


class Process:
    """A child process whose output goes to a log file."""

    def __init__(self, name: str, argv: list[str], log: Path) -> None:
        """Prepare, but do not start, ``argv``."""
        self.name = name
        self.argv = argv
        self.log = log
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        """Start the process with its output appended to the log."""
        handle = self.log.open("ab")
        self.process = await asyncio.create_subprocess_exec(
            *self.argv, stdout=handle, stderr=asyncio.subprocess.STDOUT, env=child_environment()
        )
        handle.close()

    async def stop(self, timeout: float = 15) -> int | None:
        """Send SIGTERM, wait, and kill it if it does not exit in time."""
        if self.process is None or self.process.returncode is not None:
            return None if self.process is None else self.process.returncode
        self.process.send_signal(signal.SIGTERM)
        try:
            async with asyncio.timeout(timeout):
                return await self.process.wait()
        except TimeoutError:
            self.process.kill()
            return await self.process.wait()


async def wait_until(probe: Callable[[], Awaitable[bool]], what: str, timeout: float = 20) -> None:
    """Poll ``probe`` until it returns True."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(aiohttp.ClientError, OSError):
            if await probe():
                return
        await asyncio.sleep(0.1)
    message = f"{what} did not become ready within {timeout:.0f}s"
    raise TimeoutError(message)


# --------------------------------------------------------------------------- HTTP client


@dataclass
class Turn:
    """What the client saw for one run."""

    status: int
    run_id: str | None = None
    post_ms: float = 0.0
    e2e_ms: float = 0.0
    events: list[Record] = field(default_factory=list)
    terminal: str | None = None
    data: Record = field(default_factory=dict)
    text: str = ""
    stop_ms: float | None = None


class Client:
    """Small HTTP client for the Sessions and Runs APIs."""

    def __init__(self, session: aiohttp.ClientSession, base: str) -> None:
        """Talk to the gateway at ``base``."""
        self.http = session
        self.base = base

    async def json(
        self, method: str, path: str, body: Record | None = None
    ) -> tuple[int, Record, float]:
        """Send one request; return status, JSON body (or {}) and latency in ms."""
        start = time.perf_counter()
        async with self.http.request(method, self.base + path, json=body) as response:
            payload = await response.json() if response.content_type == "application/json" else {}
            return response.status, payload, (time.perf_counter() - start) * 1000

    async def create(self, title: str) -> str:
        """Create a session and return its ID."""
        status, body, _ = await self.json("POST", "/api/sessions", {"title": title})
        if status != 201:
            message = f"create session returned {status}"
            raise RuntimeError(message)
        return str(body["id"])

    async def messages(self, session_id: str) -> tuple[list[Record], float]:
        """Read every message page; return the messages and total latency."""
        items: list[Record] = []
        after, through, total = 0, None, 0.0
        while True:
            query = f"?limit=100&after={after}" + (f"&through={through}" if through else "")
            status, body, elapsed = await self.json(
                "GET", f"/api/sessions/{session_id}/messages{query}"
            )
            total += elapsed
            if status != 200:
                message = f"read messages returned {status}"
                raise RuntimeError(message)
            items.extend(item["message"] for item in body["items"])
            through = body["through"]
            if body["next_after"] is None:
                return items, total
            after = int(body["next_after"])

    async def events(
        self, run_id: str, *, last_event_id: int | None = None
    ) -> AsyncIterator[Record]:
        """Yield SSE events, each with the client time it arrived."""
        headers = {} if last_event_id is None else {"Last-Event-ID": str(last_event_id)}
        async with self.http.get(f"{self.base}/v1/runs/{run_id}/events", headers=headers) as stream:
            data_lines: list[str] = []
            async for raw in stream.content:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("data: "):
                    data_lines.append(line[6:])
                elif not line and data_lines:
                    event = json.loads("\n".join(data_lines))
                    event["client_ms"] = time.perf_counter() * 1000
                    data_lines = []
                    yield event

    async def run(self, session_id: str, message: str, *, stop_on_text: bool = False) -> Turn:
        """Start a run and follow its events to the end."""
        start = time.perf_counter()
        status, body, post_ms = await self.json(
            "POST", "/v1/runs", {"session_id": session_id, "message": message}
        )
        turn = Turn(status=status, post_ms=post_ms)
        if status != 202:
            turn.data = body
            return turn
        turn.run_id = str(body["run_id"])
        async with contextlib.aclosing(self.events(turn.run_id)) as events:
            await self._follow(turn, events, stop_on_text=stop_on_text)
        turn.e2e_ms = (time.perf_counter() - start) * 1000
        return turn

    async def _follow(
        self, turn: Turn, events: AsyncIterator[Record], *, stop_on_text: bool
    ) -> None:
        async for event in events:
            turn.events.append({"id": event["id"], "type": event["type"], "data": event["data"]})
            if event["type"] == "text.delta":
                turn.text += str(event["data"]["text"])
                if stop_on_text and turn.stop_ms is None:
                    stopped, _, turn.stop_ms = await self.json(
                        "POST", f"/v1/runs/{turn.run_id}/stop"
                    )
                    turn.data["stop_status"] = stopped
            elif event["type"] == "retry.scheduled":
                turn.text = ""
            if event["type"] in TERMINALS:
                turn.terminal = str(event["type"])
                turn.data.update(event["data"])
                return


# --------------------------------------------------------------------------- cases


@dataclass
class Case:
    """Outcome of one scripted case."""

    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    notes: Record = field(default_factory=dict)

    def check(self, condition: bool, description: str, detail: object = "") -> bool:
        """Record one assertion without stopping the case."""
        self.checks.append((description, bool(condition), str(detail)))
        return bool(condition)

    @property
    def passed(self) -> bool:
        """True when every check held."""
        return all(ok for _, ok, _ in self.checks)


def expected_reply(history: list[Record]) -> stub_model.Reply:
    """What the stub answers after ``history`` (which ends with a user or tool message)."""
    return stub_model.plan_reply(history)


def verify_transcript(case: Case, messages: list[Record], *, partial_last: bool = False) -> None:
    """Check every assistant message against the stub's deterministic answer."""
    mismatches = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        expected = expected_reply(messages[:index])
        if message.get("tool_calls"):
            call = message["tool_calls"][0]
            wanted = expected.tool_call or {}
            if call["id"] != wanted.get("id") or call["function"]["arguments"] != wanted.get(
                "arguments"
            ):
                mismatches.append(index)
        elif partial_last and index == len(messages) - 1:
            if not expected.text.startswith(str(message.get("content") or "")):
                mismatches.append(index)
        elif message.get("content") != expected.text:
            mismatches.append(index)
    case.check(not mismatches, "stored answers match the stub", f"mismatched at {mismatches}")


def completed(case: Case, turn: Turn, revision: int | None = None) -> None:
    """Check a run was accepted and ended completed and durable."""
    case.turns.append(turn)
    case.check(turn.status == 202, "run accepted (202)", turn.status)
    case.check(turn.terminal == "run.completed", "ends with run.completed", turn.terminal)
    case.check(turn.data.get("durable") is True, "terminal says durable", turn.data.get("durable"))
    if revision is not None:
        case.check(
            turn.data.get("revision") == revision,
            f"revision is {revision}",
            turn.data.get("revision"),
        )


class Harness:
    """Everything one backend's run needs: gateway control, client and CLI."""

    def __init__(self, env_file: Path, trace: Path, log: Path, client: Client) -> None:
        """Bind the per-backend files and the HTTP client."""
        self.env_file = env_file
        self.trace = trace
        self.gateway = Process(
            "gateway",
            [
                sys.executable,
                str(HERE / "serve_gateway.py"),
                "--env-file",
                str(env_file),
                "--trace",
                str(trace),
            ],
            log,
        )
        self.client = client
        self.deleted: list[str] = []

    async def start_gateway(self) -> None:
        """Start the gateway and wait until it answers."""
        await self.gateway.start()

        async def ready() -> bool:
            status, _, _ = await self.client.json("GET", "/api/sessions?limit=1")
            return status == 200

        await wait_until(ready, "gateway")

    async def cli(self, *args: str, stdin: str) -> tuple[int, str]:
        """Run ``mertina`` with this backend's env file and scripted input."""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mertina_agent.cli",
            "--env-file",
            str(self.env_file),
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=child_environment(),
        )
        async with asyncio.timeout(60):
            output, _ = await process.communicate(stdin.encode("utf-8"))
        return int(process.returncode or 0), output.decode("utf-8", "replace")


async def case_basic(h: Harness) -> Case:
    """Three turns, SSE replay with Last-Event-ID, and read-back."""
    case = Case("basic multi-turn + SSE replay")
    sid = await h.client.create("e2e-01-basic")
    for index, message in enumerate(
        ["Hello Mertina", "What did I just say?", "Thanks, that is all."], 1
    ):
        turn = await h.client.run(sid, message)
        completed(case, turn, revision=index)
    last = case.turns[-1]
    replayed = [event async for event in h.client.events(str(last.run_id), last_event_id=2)]
    case.check(
        bool(replayed)
        and all(int(event["id"]) > 2 for event in replayed)
        and replayed[-1]["type"] == "run.completed",
        "Last-Event-ID replays only later events",
        [event["type"] for event in replayed],
    )
    messages, read_ms = await h.client.messages(sid)
    case.notes["read_ms"] = read_ms
    case.check(len(messages) == 6, "6 messages stored", len(messages))
    case.check([m["role"] for m in messages] == ["user", "assistant"] * 3, "roles alternate")
    verify_transcript(case, messages)
    case.check(
        all(t.text == messages[2 * i + 1]["content"] for i, t in enumerate(case.turns)),
        "streamed text equals stored text",
    )
    _, session, _ = await h.client.json("GET", f"/api/sessions/{sid}")
    case.check(
        (session.get("history_revision"), session.get("message_count")) == (3, 6),
        "session revision 3, 6 messages",
        (session.get("history_revision"), session.get("message_count")),
    )
    return case


async def case_unicode(h: Harness) -> Case:
    """Mixed scripts and emoji in, more than 64 KiB of Unicode out."""
    case = Case("Unicode + 100 KiB answer")
    sid = await h.client.create("e2e-02-unicode")
    message = "[long] 你好 世界 🌏 — naïve café · 👩‍👩‍👧 · עברית · " * 40
    turn = await h.client.run(sid, message)
    completed(case, turn, revision=1)
    messages, read_ms = await h.client.messages(sid)
    case.notes["read_ms"] = read_ms
    answer = str(messages[1].get("content") or "") if len(messages) > 1 else ""
    case.check(messages[0].get("content") == message, "user text stored byte-for-byte")
    case.check(answer == stub_model.long_text(), "long answer stored byte-for-byte")
    case.check(
        len(answer.encode("utf-8")) > 65536, "answer exceeds 64 KiB", len(answer.encode("utf-8"))
    )
    case.check(turn.text == answer, "streamed text equals stored text")
    return case


async def case_tool(h: Harness) -> Case:
    """A tool call and its result are stored in order and linked."""
    case = Case("tool call")
    sid = await h.client.create("e2e-03-tool")
    turn = await h.client.run(sid, "[tool] Mertina release plan")
    completed(case, turn, revision=1)
    types = [event["type"] for event in turn.events]
    case.check(
        "tool.started" in types and "tool.finished" in types, "SSE reports the tool call", types
    )
    messages, read_ms = await h.client.messages(sid)
    case.notes["read_ms"] = read_ms
    roles = [m["role"] for m in messages]
    case.check(
        roles == ["user", "assistant", "tool", "assistant"],
        "user → tool call → result → answer",
        roles,
    )
    if roles == ["user", "assistant", "tool", "assistant"]:
        call = messages[1]["tool_calls"][0]
        case.check(call["id"] == messages[2]["tool_call_id"], "tool_call_id links call and result")
        case.check(
            messages[3]["content"] == f"Lookup finished: {messages[2]['content']}",
            "answer uses the tool result",
        )
    verify_transcript(case, messages)
    return case


async def case_stop(h: Harness) -> Case:
    """Stopping mid-reply saves the partial answer as a stopped turn."""
    case = Case("stop mid-reply")
    sid = await h.client.create("e2e-04-stop")
    turn = await h.client.run(sid, "[slow] Tell me a long story", stop_on_text=True)
    case.turns.append(turn)
    case.check(
        turn.data.get("stop_status") == 200, "stop request accepted", turn.data.get("stop_status")
    )
    case.check(turn.terminal == "run.stopped", "ends with run.stopped", turn.terminal)
    case.check(
        turn.data.get("durable") is True, "stopped turn is durable", turn.data.get("durable")
    )
    case.check(
        turn.e2e_ms < SLOW_TOTAL_S * 1000, "stopped before the full answer", f"{turn.e2e_ms:.0f}ms"
    )
    messages, _ = await h.client.messages(sid)
    partial = str(messages[-1].get("content") or "") if messages else ""
    full = expected_reply(messages[:1]).text if messages else ""
    case.check(
        [m["role"] for m in messages] == ["user", "assistant"], "user + partial answer stored"
    )
    case.check(
        partial.startswith("word0") and len(partial) < len(full),
        "partial answer is a prefix",
        f"{len(partial)}/{len(full)} chars",
    )
    verify_transcript(case, messages, partial_last=True)
    return case


async def case_restart(h: Harness) -> Case:
    """History survives a gateway restart and continues from the CLI."""
    case = Case("restart + CLI --resume")
    sid = await h.client.create("e2e-05-restart")
    completed(case, await h.client.run(sid, "Remember the number 42."), revision=1)
    start = time.perf_counter()
    code = await h.gateway.stop()
    await h.start_gateway()
    case.notes["restart_ms"] = (time.perf_counter() - start) * 1000
    case.check(code == 0, "gateway exits cleanly on SIGTERM", code)
    status, session, _ = await h.client.json("GET", f"/api/sessions/{sid}")
    case.check(
        status == 200 and session.get("message_count") == 2,
        "session survives the restart",
        session.get("message_count"),
    )
    turn = await h.client.run(sid, "What number did I mention?")
    completed(case, turn, revision=2)
    case.check(turn.text.startswith("Reply 2:"), "next turn sees the saved history", turn.text[:12])
    code, output = await h.cli("chat", "--resume", sid, stdin="And once more from the CLI.\n")
    case.check(
        code == 0 and "Reply 3:" in output,
        "CLI --resume continues the same session",
        output.strip()[-80:],
    )
    messages, _ = await h.client.messages(sid)
    case.check(len(messages) == 6, "6 messages after gateway + CLI turns", len(messages))
    verify_transcript(case, messages)
    return case


async def case_concurrency(h: Harness) -> Case:
    """Two sessions run at once; a second run in a busy session gets 409."""
    case = Case("two sessions at once + 409")
    first, second = (
        await h.client.create("e2e-06-concurrent-a"),
        await h.client.create("e2e-06-concurrent-b"),
    )
    start = time.perf_counter()
    runs = [
        asyncio.create_task(h.client.run(sid, "[slow] Count for me")) for sid in (first, second)
    ]
    await asyncio.sleep(0.3)
    _, session, _ = await h.client.json("GET", f"/api/sessions/{first}")
    case.check(
        bool(session.get("active_run_id")),
        "session shows its active run",
        session.get("active_run_id"),
    )
    conflict, _, _ = await h.client.json(
        "POST", "/v1/runs", {"session_id": first, "message": "Me too"}
    )
    case.check(conflict == 409, "second run in a busy session gets 409", conflict)
    turns = await asyncio.gather(*runs)
    wall = time.perf_counter() - start
    for turn in turns:
        completed(case, turn, revision=1)
    case.check(
        wall < SLOW_TOTAL_S * 1.6,
        "sessions ran concurrently",
        f"{wall:.2f}s for two {SLOW_TOTAL_S:.1f}s runs",
    )
    for sid in (first, second):
        messages, _ = await h.client.messages(sid)
        case.check(len(messages) == 2, "no extra rows from the rejected run", len(messages))
    return case


async def case_rename_delete(h: Harness) -> Case:
    """Rename checks the metadata version; delete removes everything."""
    case = Case("rename + delete")
    sid = await h.client.create("e2e-07-rename")
    completed(case, await h.client.run(sid, "Short chat before rename."), revision=1)
    status, body, _ = await h.client.json(
        "PATCH", f"/api/sessions/{sid}", {"title": "e2e-07-renamed", "expected_version": 0}
    )
    case.check(
        status == 200 and body.get("metadata_version") == 1,
        "rename bumps metadata_version",
        (status, body.get("metadata_version")),
    )
    status, _, _ = await h.client.json(
        "PATCH", f"/api/sessions/{sid}", {"title": "again", "expected_version": 0}
    )
    case.check(status == 409, "stale expected_version gets 409", status)
    status, _, _ = await h.client.json("DELETE", f"/api/sessions/{sid}")
    case.check(status == 204, "delete returns 204", status)
    status, _, _ = await h.client.json("GET", f"/api/sessions/{sid}")
    case.check(status == 404, "deleted session is gone", status)
    status, _, _ = await h.client.json("GET", f"/api/sessions/{sid}/messages")
    case.check(status == 404, "its messages are gone", status)
    h.deleted.append(sid)
    return case


async def case_cross_entry(h: Harness) -> Case:
    """A session written by the CLI reads back the same through the gateway."""
    case = Case("CLI write, gateway read")
    title = "[cli] hello from the CLI"
    code, output = await h.cli(stdin=f"{title}\n/exit\n")
    case.check(code == 0 and "Reply 1:" in output, "CLI chat succeeds", output.strip()[-80:])
    _, body, _ = await h.client.json("GET", "/api/sessions?limit=100")
    found = [item for item in body.get("items", []) if item["title"] == title]
    case.check(len(found) == 1, "gateway lists the CLI session", len(found))
    if found:
        messages, _ = await h.client.messages(str(found[0]["id"]))
        case.check(len(messages) == 2, "gateway reads both messages", len(messages))
        verify_transcript(case, messages)
    return case


CASES = (
    case_basic,
    case_unicode,
    case_tool,
    case_stop,
    case_restart,
    case_concurrency,
    case_rename_delete,
    case_cross_entry,
)


# --------------------------------------------------------------------------- trace


class TraceReader:
    """Read new records from the gateway's trace file as they are appended."""

    def __init__(self, path: Path) -> None:
        """Start at the beginning of ``path``."""
        self.path = path
        self.offset = 0
        self.turns: dict[str, Record] = {}
        self.db: list[Record] = []

    def poll(self) -> None:
        """Consume complete lines written since the last poll."""
        if not self.path.exists():
            return
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
        complete = chunk[: chunk.rfind(b"\n") + 1]
        self.offset += len(complete)
        for raw in complete.splitlines():
            record = json.loads(raw)
            if record.get("kind") == "turn":
                self.turns[str(record["run_id"])] = record
            elif record.get("kind") == "db":
                self.db.append(record)


async def growth(h: Harness, reader: TraceReader, total: int) -> list[Record]:
    """Grow one session turn by turn, recording server and client timings."""
    sid = await h.client.create("e2e-growth")
    points: list[Record] = []
    started = time.monotonic()
    filler = "context " * 25
    for number in range(1, total + 1):
        CONSOLE.doing(f"growth turn {number}/{total}")
        turn = await h.client.run(sid, f"Growth turn {number}: {filler}")
        reader.poll()
        server = reader.turns.get(str(turn.run_id), {})
        point: Record = {
            "turn": number,
            "terminal": turn.terminal,
            "history_messages": server.get("history_messages"),
            "history_bytes": server.get("history_bytes"),
            "load_ms": server.get("load_ms"),
            "commit_ms": server.get("commit_ms"),
            "agent_ms": server.get("agent_ms"),
            "post_ms": turn.post_ms,
            "e2e_ms": turn.e2e_ms,
        }
        if number in MILESTONES or number == total:
            _, read_ms = await h.client.messages(sid)
            point["read_all_ms"] = read_ms
            _, _, first_page_ms = await h.client.json(
                "GET", f"/api/sessions/{sid}/messages?limit=50"
            )
            point["read_page_ms"] = first_page_ms
        points.append(point)
        CONSOLE.bar(
            "growth",
            number,
            total,
            f"history {server.get('history_messages', '?')} msgs · "
            f"load {fmt_ms(server.get('load_ms'))} "
            f"· commit {fmt_ms(server.get('commit_ms'))} · {time.monotonic() - started:.0f}s",
        )
        if turn.terminal != "run.completed":
            CONSOLE.line(
                CONSOLE.bad(f"  growth stopped at turn {number}: {turn.terminal} {turn.data}")
            )
            break
    return points


# --------------------------------------------------------------------------- backends


def backend_settings(name: str, directory: Path) -> tuple[str, Settings]:
    """Database URL and settings for one backend's run."""
    if name == "sqlite":
        url = f"sqlite+aiosqlite:///{directory / 'mertina.sqlite3'}"
    else:
        url = os.environ.get(URL_VARIABLES[name]) or DEFAULT_URLS[name]
    return url, Settings(_env_file=None, database_url=SecretStr(url))


async def reset(settings: Settings) -> str:
    """Drop the session tables, if any, and return the server version."""
    schema = Schema()
    async with await open_database(settings) as database, database.transaction() as connection:
        await connection.run_sync(schema.metadata.drop_all)
        if database.backend == "sqlite":
            query = "SELECT sqlite_version()"
        elif database.backend == "postgresql":
            query = "SHOW server_version"
        else:
            query = "SELECT VERSION()"
        return str((await connection.execute(text(query))).scalar_one()).split(" ")[0]


def write_env(path: Path, url: str, stub_port: int, gateway_port: int) -> None:
    """The env file every process of this backend's run reads."""
    path.write_text(
        "\n".join(
            [
                f"MERTINA_LLM_BASE_URL=http://127.0.0.1:{stub_port}/v1",
                "MERTINA_LLM_MODEL=e2e-stub",
                "MERTINA_LLM_STREAM=true",
                "MERTINA_LLM_MAX_ATTEMPTS=1",
                "MERTINA_LOG_LEVEL=WARNING",
                f"MERTINA_DATABASE_URL={url}",
                f"MERTINA_GATEWAY_PORT={gateway_port}",
                "MERTINA_GATEWAY_SHUTDOWN_TIMEOUT_S=10",
                "",
            ]
        ),
        encoding="utf-8",
    )


def stats(values: list[float]) -> Record:
    """Median, p95 and max of a series."""
    return {
        "n": len(values),
        "p50": statistics.median(values) if values else None,
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


async def run_backend(
    name: str, index: int, count: int, run_dir: Path, stub_port: int, growth_turns: int
) -> Record:
    """Run every case and the growth curve on one backend."""
    directory = run_dir / name
    directory.mkdir(parents=True)
    CONSOLE.line()
    CONSOLE.line(CONSOLE.bold(f"▶ [{index}/{count}] {LABELS[name]}"))
    url, settings = backend_settings(name, directory)
    summary: Record = {"backend": name, "label": LABELS[name]}
    try:
        async with step("reset database") as note:
            version = await reset(settings)
            summary["version"] = version
            note["detail"] = f"{LABELS[name]} {version}"
    except Exception as exc:  # unreachable server, wrong credentials
        summary["skipped"] = f"{type(exc).__name__}: {exc}"
        CONSOLE.line(CONSOLE.bad(f"  skipped: {summary['skipped']}"))
        return summary

    env_file = directory / "e2e.env"
    trace = directory / "trace.jsonl"
    gateway_port = free_port()
    write_env(env_file, url, stub_port, gateway_port)
    base = f"http://127.0.0.1:{gateway_port}"
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        h = Harness(env_file, trace, directory / "gateway.log", Client(http, base))
        async with step("mertina db init"):
            code, output = await h.cli("db", "init", stdin="")
            if code != 0:
                raise RuntimeError(output.strip())
        async with step("start gateway") as note:
            await h.start_gateway()
            note["detail"] = base
        reader = TraceReader(trace)
        cases: list[Case] = []
        for number, run_case in enumerate(CASES, 1):
            CONSOLE.doing(f"case {number}: {run_case.__name__}")
            start = time.perf_counter()
            try:
                case = await run_case(h)
            except Exception as exc:
                case = Case(run_case.__name__)
                case.check(False, "case raised", f"{type(exc).__name__}: {exc}")
            elapsed = time.perf_counter() - start
            cases.append(case)
            reader.poll()
            served = [reader.turns.get(str(t.run_id), {}) for t in case.turns if t.run_id]
            loads = [float(s["load_ms"]) for s in served if s.get("load_ms") is not None]
            commits = [float(s["commit_ms"]) for s in served if s.get("commit_ms") is not None]
            mark = CONSOLE.ok("✓") if case.passed else CONSOLE.bad("✗")
            detail = f"{len(case.checks)} checks · {len(case.turns)} turns"
            if loads:
                detail += f" · load {fmt_ms(statistics.median(loads))}"
                detail += f" · commit {fmt_ms(statistics.median(commits))}"
            CONSOLE.line(
                f"  [{number}/{len(CASES)}] {case.name:<30} {mark} "
                f"{elapsed:5.2f}s  {CONSOLE.dim(detail)}"
            )
            for description, ok, info in case.checks:
                if not ok:
                    CONSOLE.line(CONSOLE.bad(f"        ✗ {description}: {info}"))
        points = await growth(h, reader, growth_turns)
        async with step("stop gateway") as note:
            note["detail"] = f"exit {await h.gateway.stop()}"
        reader.poll()
        async with step("inspect database") as note:
            inspection = await inspect_db.inspect(settings, deleted_ids=h.deleted)
            note["detail"] = (
                f"{len(inspection['sessions'])} sessions · {inspection['messages']} messages · "
                f"{len(inspection['problems'])} problems · "
                f"{inspection['storage_bytes'] / 1024:.0f} KiB"
            )
        for problem in inspection["problems"]:
            CONSOLE.line(CONSOLE.bad(f"        ✗ {problem}"))

    turns = list(reader.turns.values())
    ops: dict[str, list[float]] = {}
    for record in reader.db:
        ops.setdefault(str(record["op"]), []).append(float(record["ms"]))
    summary.update(
        {
            "cases": [
                {"name": c.name, "passed": c.passed, "checks": c.checks, "notes": c.notes}
                for c in cases
            ],
            "phases": {
                key: stats([float(t[key]) for t in turns if t.get(key) is not None])
                for key in ("load_ms", "agent_ms", "ttft_ms", "commit_ms")
            },
            "client": {
                "post_ms": stats([float(p["post_ms"]) for p in points]),
                "e2e_ms": stats([float(p["e2e_ms"]) for p in points]),
            },
            "db_ops": {op: stats(values) for op, values in sorted(ops.items())},
            "growth": points,
            "inspection": inspection,
        }
    )
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed = sum(1 for c in cases if c.passed)
    last = points[-1] if points else {}
    phases = summary["phases"]
    CONSOLE.line(
        "  "
        + CONSOLE.bold("Summary: ")
        + f"{passed}/{len(cases)} cases · load p50 {fmt_ms(phases['load_ms']['p50'])} · "
        f"commit p50 {fmt_ms(phases['commit_ms']['p50'])} · "
        f"@{last.get('turn', 0)} turns load {fmt_ms(last.get('load_ms'))} "
        f"commit {fmt_ms(last.get('commit_ms'))}"
    )
    return summary


async def main_async(args: argparse.Namespace) -> int:
    """Run the selected backends and write the report."""
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS / run_id
    run_dir.mkdir(parents=True)
    CONSOLE.line(CONSOLE.bold(f"═══ Mertina v0.2 end-to-end · run {run_id} ═══"))
    CONSOLE.line(
        f"Backends: {', '.join(LABELS[b] for b in args.backends)} · "
        f"growth {args.growth_turns} turns"
    )
    CONSOLE.line(f"Results:  {run_dir.relative_to(ROOT)}")
    heartbeat = asyncio.create_task(CONSOLE.heartbeat())
    stub_port = free_port()
    stub = Process(
        "stub",
        [sys.executable, str(HERE / "stub_model.py"), "--port", str(stub_port)],
        run_dir / "stub.log",
    )
    summaries: list[Record] = []
    try:
        async with step("start stub model") as note:
            await stub.start()

            async def ready() -> bool:
                with socket.create_connection(("127.0.0.1", stub_port), timeout=0.2):
                    return True

            await wait_until(ready, "stub model")
            note["detail"] = f"http://127.0.0.1:{stub_port}"
        for index, name in enumerate(args.backends, 1):
            summaries.append(
                await run_backend(
                    name, index, len(args.backends), run_dir, stub_port, args.growth_turns
                )
            )
    finally:
        await stub.stop()
        heartbeat.cancel()
    (run_dir / "run.json").write_text(
        json.dumps(
            {"run_id": run_id, "backends": args.backends, "growth_turns": args.growth_turns}
        ),
        encoding="utf-8",
    )
    CONSOLE.line()
    CONSOLE.line(CONSOLE.bold("═══ Comparison ═══"))
    comparison = report.compare(summaries)
    for row in report.console_rows(summaries, comparison):
        CONSOLE.line("  " + row)
    if not args.no_report:
        path = report.write(
            run_id, summaries, comparison, ROOT / "docs" / "testing" / "v0.2-e2e" / "README.md"
        )
        CONSOLE.line(f"Report: {path.relative_to(ROOT)}")
    failures = sum(1 for s in summaries for c in s.get("cases", []) if not c["passed"]) + sum(
        len(s.get("inspection", {}).get("problems", [])) for s in summaries
    )
    ok = failures == 0 and comparison["identical"] and not any("skipped" in s for s in summaries)
    CONSOLE.line(
        CONSOLE.ok("All checks passed.")
        if ok
        else CONSOLE.bad(f"Finished with {failures} failure(s).")
    )
    return 0 if ok else 1


def main() -> int:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(description="Mertina v0.2 end-to-end runner.")
    parser.add_argument(
        "--backends", default="sqlite,postgresql,mysql", type=lambda v: v.split(",")
    )
    parser.add_argument("--growth-turns", type=int, default=200)
    parser.add_argument("--no-report", action="store_true", help="do not write the docs report")
    args = parser.parse_args()
    unknown = set(args.backends) - set(LABELS)
    if unknown:
        parser.error(f"unknown backends: {', '.join(sorted(unknown))}")
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        CONSOLE.line(CONSOLE.bad("Interrupted."))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
