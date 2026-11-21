"""Start the gateway for end-to-end tests, with a deterministic tool and timing probes.

Same as ``mertina gateway`` except for two test-only changes made in this
process, without touching the product code:

- Agents get a deterministic ``lookup`` tool instead of ``web_search``, so tool
  turns never reach the network and store identical results on every backend.
- ``SessionDB`` methods and agent turns are wrapped with timers. Each committed
  turn appends one ``turn`` record (history load, agent run, time to first text,
  tool calls, commit) to the ``--trace`` JSONL file; other storage calls append
  ``db`` records. Records carry sizes and timings only, never message text.

Run with ``python dev/e2e/serve_gateway.py --env-file E2E.env --trace trace.jsonl``.
"""

import argparse
import asyncio
import contextvars
import functools
import json
import logging
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from mertina_agent.agent.core import Agent
from mertina_agent.agent.events import AgentEvent, RetryScheduled, TextDelta, ToolCallFinished
from mertina_agent.agent.transports.types import ChatMessage
from mertina_agent.agent.turn_result import ConversationResult
from mertina_agent.config import Settings, load_settings
from mertina_agent.gateway.run import GatewayRunner
from mertina_agent.state.models import CommitReceipt, HistorySnapshot, TurnCommit
from mertina_agent.state.serialization import encode_message, turn_status
from mertina_agent.state.session_db import SessionDB
from mertina_agent.tools.registry import ToolRegistry, tool_result

type Record = dict[str, object]

LOOKUP_SCHEMA = {
    "name": "lookup",
    "description": "Look up a fact. Deterministic test tool.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}

# Set by the history-load wrapper inside a run's own task, so the agent and
# commit wrappers that run later in the same task add to the same record.
TURN: contextvars.ContextVar[Record | None] = contextvars.ContextVar("turn", default=None)


class Trace:
    """Append-only JSONL sink, flushed per record so a crash keeps earlier data."""

    def __init__(self, path: Path) -> None:
        """Open ``path`` for appending."""
        self._file = path.open("a", encoding="utf-8")

    def emit(self, record: Record) -> None:
        """Write one record with a wall-clock timestamp."""
        self._file.write(json.dumps({"ts": time.time(), **record}) + "\n")
        self._file.flush()


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def _bytes(messages: Sequence[ChatMessage]) -> int:
    return sum(len(encode_message(message).encode("utf-8")) for message in messages)


def lookup(arguments: dict[str, object]) -> str:
    """Deterministic tool: describe the query without any I/O."""
    query = str(arguments.get("query", ""))
    return tool_result(query=query, answer=f"{len(query)} characters, deterministic result")


def instrument(trace: Trace) -> None:
    """Wrap SessionDB so every storage call made by this process is timed."""
    load_history = SessionDB.load_history
    commit_turn = SessionDB.commit_turn

    @functools.wraps(load_history)
    async def timed_load(self: SessionDB, session_id: str) -> HistorySnapshot:
        start = time.perf_counter()
        snapshot = await load_history(self, session_id)
        elapsed = _ms(start)
        TURN.set(
            {
                "session_id": session_id,
                "load_ms": elapsed,
                "history_messages": len(snapshot.messages),
                "history_bytes": _bytes(snapshot.messages),
            }
        )
        return snapshot

    @functools.wraps(commit_turn)
    async def timed_commit(self: SessionDB, command: TurnCommit) -> CommitReceipt:
        turn: Record = {"session_id": command.session_id, **(TURN.get() or {})}
        turn.update(kind="turn", run_id=command.operation_id, status=command.status)
        turn.update(
            written_messages=len(command.payloads),
            written_bytes=sum(len(payload.encode("utf-8")) for payload in command.payloads),
        )
        start = time.perf_counter()
        try:
            receipt = await commit_turn(self, command)
        except Exception as exc:
            trace.emit({**turn, "commit_ms": _ms(start), "commit_error": type(exc).__name__})
            raise
        trace.emit({**turn, "commit_ms": _ms(start), "revision": receipt.revision})
        return receipt

    def timed(
        op: str, method: Callable[..., Awaitable[object]]
    ) -> Callable[..., Awaitable[object]]:
        @functools.wraps(method)
        async def wrapper(self: SessionDB, *args: object, **kwargs: object) -> object:
            start = time.perf_counter()
            try:
                return await method(self, *args, **kwargs)
            finally:
                trace.emit({"kind": "db", "op": op, "ms": _ms(start)})

        return wrapper

    SessionDB.load_history = timed_load  # type: ignore[method-assign]
    SessionDB.commit_turn = timed_commit  # type: ignore[method-assign]
    for op in (
        "create_session",
        "get_session",
        "list_sessions",
        "rename_session",
        "delete_session",
        "read_messages",
    ):
        setattr(SessionDB, op, timed(op, getattr(SessionDB, op)))


class TimedAgent(Agent):
    """Agent that records its run time, first text, tool calls and retries."""

    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        callback: Callable[[AgentEvent], None],
    ) -> None:
        """Forward every event to the gateway's ``callback`` after noting it."""
        self._outer = callback
        self._first_text: float | None = None
        self._tool_ms: list[float] = []
        self._retries = 0
        super().__init__(settings, tool_registry=registry, event_callback=self._observe)

    def _observe(self, event: AgentEvent) -> None:
        if isinstance(event, TextDelta) and self._first_text is None:
            self._first_text = time.perf_counter()
        elif isinstance(event, ToolCallFinished):
            self._tool_ms.append(round(event.duration_s * 1000, 3))
        elif isinstance(event, RetryScheduled):
            self._retries += 1
            self._first_text = None
        self._outer(event)

    async def run_conversation(
        self, user_message: str, *, conversation_history: Sequence[ChatMessage] | None = None
    ) -> ConversationResult:
        """Run the turn and add its timings to the current trace record."""
        started = time.perf_counter()
        result = await super().run_conversation(
            user_message, conversation_history=conversation_history
        )
        turn = TURN.get()
        if turn is not None:
            turn.update(
                agent_ms=_ms(started),
                ttft_ms=None
                if self._first_text is None
                else round((self._first_text - started) * 1000, 3),
                tool_calls=len(self._tool_ms),
                tool_ms=self._tool_ms,
                retries=self._retries,
                api_calls=result["api_calls"],
                agent_status=turn_status(result),
            )
        return result


async def serve(settings: Settings, trace: Trace) -> None:
    """Listen until SIGTERM or SIGINT, then shut the gateway down gracefully."""
    registry = ToolRegistry()
    registry.register("lookup", "e2e", LOOKUP_SCHEMA, lookup)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    loop.add_signal_handler(signal.SIGINT, stopped.set)
    async with GatewayRunner(
        settings, agent_factory=lambda callback: TimedAgent(settings, registry, callback)
    ) as runner:
        trace.emit({"kind": "gateway", "event": "listening", "address": runner.address})
        sys.stdout.write(f"listening {runner.address}\n")
        sys.stdout.flush()
        await stopped.wait()
    trace.emit({"kind": "gateway", "event": "stopped"})


def main() -> None:
    """Parse arguments, install the probes and serve."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    args = parser.parse_args()
    settings = load_settings(env_file=args.env_file)
    logging.basicConfig(level=logging.WARNING)
    trace = Trace(args.trace)
    instrument(trace)
    asyncio.run(serve(settings, trace))


if __name__ == "__main__":
    main()
