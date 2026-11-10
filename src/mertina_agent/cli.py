"""Developer command line: chat in a saved session, list sessions, run the gateway.

Installed as ``mertina``. A bare ``mertina`` (or ``mertina chat``, alias
``mertina run``) opens an interactive conversation that runs the agent in this
process and commits every finished turn to the configured database, so it can be
resumed later from the CLI or the gateway. ``mertina gateway`` and ``mertina db``
wrap the existing ``python -m`` entry points.

Settings come from ``--env-file`` when given, otherwise from ``./.env`` and then
``~/.mertina/.env``, whichever exists first; environment variables always win.

This is a development tool, not the P2 local client: it has no TUI, keeps one
session open at a time and relies on the database's revision check, not a
cross-process lock, if the gateway writes the same session concurrently.
"""

import argparse
import asyncio
import signal
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from mertina_agent.agent.events import (
    AgentEvent,
    RetryScheduled,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from mertina_agent.config import Settings, load_settings
from mertina_agent.database.runtime import open_database
from mertina_agent.exceptions import ConfigurationError, MertinaError
from mertina_agent.gateway.session import AgentFactory, SessionAgent, build_agent_factory
from mertina_agent.state.models import SessionRecord, TurnStatus
from mertina_agent.state.schema import Schema
from mertina_agent.state.serialization import build_commit, turn_status
from mertina_agent.state.session_db import SessionDB

TITLE_CHARS = 60
"""Length of the title taken from a new session's first message."""

HELP = """Commands:
  /new          start a new conversation
  /sessions     list recent sessions
  /resume ID    continue a saved session
  /help         show this help
  /exit         leave (Ctrl+D also works)
Ctrl+C stops the current reply; press it again to abandon the turn without saving it.
"""


def resolve_env_file(explicit: Path | None, *, home: Path | None = None) -> Path | None:
    """Pick the env file: an explicit one must exist; otherwise the first one found.

    Args:
        explicit: The ``--env-file`` argument, if any.
        home: Home directory override for tests.

    Returns:
        The file to read, or ``None`` to use the process environment only.

    Raises:
        ConfigurationError: If an explicit env file does not exist.
    """
    if explicit is not None:
        if not explicit.is_file():
            message = f"Env file not found: {explicit}"
            raise ConfigurationError(message)
        return explicit
    base = home if home is not None else Path.home()
    for candidate in (Path(".env"), base / ".mertina" / ".env"):
        if candidate.is_file():
            return candidate
    return None


def _printable(text: str) -> str:
    """Escape control characters so model output cannot drive the terminal."""
    return "".join(
        char if char in "\n\t" or char.isprintable() else repr(char)[1:-1] for char in text
    )


def _title(message: str) -> str:
    line = " ".join(message.split())
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1] + "…"


def format_session(session: SessionRecord) -> str:
    """One line per session: ID, last update in local time, size and title."""
    updated = datetime.fromtimestamp(session.updated_at_us / 1_000_000).strftime("%Y-%m-%d %H:%M")
    return f"{session.id}  {updated}  {session.message_count:>4} msgs  {_printable(session.title)}"


async def recent_sessions(store: SessionDB, limit: int) -> list[SessionRecord]:
    """Return the most recently updated sessions, newest first."""
    sessions: list[SessionRecord] = []
    after: tuple[int, str] | None = None
    while True:
        page = await store.list_sessions(limit=100, after=after)
        sessions.extend(page)
        if len(page) < 100:
            break
        after = (page[-1].created_at_us, page[-1].id)
    sessions.sort(key=lambda session: session.updated_at_us, reverse=True)
    return sessions[:limit]


class ChatSession:
    """One interactive conversation: run a turn in-process, then commit it.

    Each turn reloads the saved history, so a session continued elsewhere is
    picked up, and uses a fresh agent, as the gateway does.
    """

    def __init__(self, store: SessionDB, factory: AgentFactory, out: TextIO) -> None:
        """Borrow an open store; ``out`` receives the streamed reply and progress."""
        self.store = store
        self._factory = factory
        self._out = out
        self.session_id: str | None = None
        self._agent: SessionAgent | None = None
        self._printed = False
        self.committing = False

    async def resume(self, session_id: str) -> SessionRecord:
        """Continue a saved session.

        Raises:
            SessionNotFoundError: If no session has this ID.
        """
        session = await self.store.get_session(session_id)
        self.session_id = session.id
        return session

    def new(self) -> None:
        """Start a new conversation; it is created when its first message is sent."""
        self.session_id = None

    def interrupt(self) -> bool:
        """Ask the running turn to stop; ``False`` if no agent is running yet."""
        return self._agent is not None and self._agent.interrupt("Stopped from the CLI")

    async def send(self, message: str) -> TurnStatus:
        """Run one turn, print it as it arrives and commit the result.

        Returns:
            How the turn ended.
        """
        if self.session_id is None:
            self.session_id = (await self.store.create_session(_title(message))).id
        snapshot = await self.store.load_history(self.session_id)
        agent = self._factory(self._report)
        self._agent = agent
        self._printed = False
        try:
            result = await agent.run_conversation(message, conversation_history=snapshot.messages)
        finally:
            self._agent = None
            await agent.aclose()
        status = turn_status(result)
        if not self._printed and result["final_response"]:
            self._write(_printable(result["final_response"]))
        self._write("\n")
        command = build_commit(
            snapshot,
            result["messages"],
            operation_id=uuid4().hex,
            status=status,
            timestamp_us=time.time_ns() // 1000,
        )
        self.committing = True
        try:
            await self.store.commit_turn(command)
        finally:
            self.committing = False
        if status != "completed":
            self._write(f"[{status}: {result['turn_exit_reason']}]\n")
        return status

    def _write(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()

    def _report(self, event: AgentEvent) -> None:
        if isinstance(event, TextDelta):
            self._printed = True
            self._write(_printable(event.text))
        elif isinstance(event, RetryScheduled):
            # The next attempt starts its answer again from the beginning.
            self._printed = False
            self._write(f"\n[retrying in {event.wait_s:.1f}s: {_printable(event.reason)}]\n")
        elif isinstance(event, ToolCallStarted):
            self._write(f"\n[tool {event.name}]\n")
        elif isinstance(event, ToolCallFinished):
            outcome = "error" if event.is_error else "ok"
            self._write(f"[tool {event.name} {outcome} in {event.duration_s:.2f}s]\n")


async def run_turn(chat: ChatSession, message: str, out: TextIO) -> None:
    """Send one message; the first Ctrl+C stops the reply, the second abandons it."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    presses = 0

    def on_interrupt() -> None:
        nonlocal presses
        presses += 1
        if presses == 1 and chat.interrupt():
            out.write("\n[stopping…]\n")
        elif task is not None:
            task.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, on_interrupt)
        installed = True
    except (NotImplementedError, RuntimeError):
        # Windows and non-main threads: Ctrl+C keeps its default behaviour.
        installed = False
    try:
        await chat.send(message)
    except asyncio.CancelledError:
        if task is None or task.uncancel() > 0:
            raise
        if chat.committing:
            out.write("\n[turn abandoned while saving; check it with /sessions]\n")
        else:
            out.write("\n[turn abandoned; nothing was saved]\n")
    except MertinaError as exc:
        out.write(f"\n[error: {exc}]\n")
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)


async def run_command(chat: ChatSession, line: str, out: TextIO) -> bool:
    """Handle a slash command; ``False`` means leave the conversation."""
    name, _, argument = line.partition(" ")
    argument = argument.strip()
    if name in {"/exit", "/quit"}:
        return False
    if name == "/new":
        chat.new()
        out.write("Started a new conversation.\n")
    elif name == "/sessions":
        sessions = await recent_sessions(chat.store, 20)
        out.write(
            "".join(format_session(session) + "\n" for session in sessions) or "No sessions.\n"
        )
    elif name == "/resume" and argument:
        try:
            session = await chat.resume(argument)
        except MertinaError:
            out.write(f"No session {argument}.\n")
        else:
            out.write(f"Resumed {session.id} ({session.message_count} messages).\n")
    elif name == "/help":
        out.write(HELP)
    else:
        out.write("Unknown command; /help lists them.\n")
    return True


def repl(
    runner: asyncio.Runner,
    chat: ChatSession,
    out: TextIO,
    read_line: Callable[[str], str] | None = None,
) -> None:
    """Read lines (with :func:`input` by default) until /exit, Ctrl+D or Ctrl+C at the prompt."""
    read = read_line if read_line is not None else input
    while True:
        try:
            line = read("you> ")
        except (EOFError, KeyboardInterrupt):
            out.write("\n")
            return
        text = line.strip()
        if not text:
            continue
        if text.startswith("/"):
            if not runner.run(run_command(chat, text, out)):
                return
            continue
        out.write("mertina> ")
        out.flush()
        runner.run(run_turn(chat, line, out))


def _chat(settings: Settings, resume: str | None, out: TextIO) -> int:
    with asyncio.Runner() as runner:
        database = runner.run(open_database(settings))
        try:
            schema = Schema()
            runner.run(schema.initialize(database, bootstrap=False))
            chat = ChatSession(
                SessionDB(database, schema=schema), build_agent_factory(settings), out
            )
            state = "new conversation"
            if resume is not None:
                session = runner.run(chat.resume(resume))
                state = f"session {session.id}, {session.message_count} messages"
            out.write(
                f"Mertina · {_printable(settings.llm_model)} · {database.backend} · {state}\n"
                "/help for commands, Ctrl+C stops a reply, Ctrl+D exits.\n"
            )
            repl(runner, chat, out)
        finally:
            runner.run(database.aclose())
    return 0


def _sessions(settings: Settings, limit: int, out: TextIO) -> int:
    async def run() -> None:
        async with await open_database(settings) as database:
            schema = Schema()
            await schema.initialize(database, bootstrap=False)
            sessions = await recent_sessions(SessionDB(database, schema=schema), limit)
            out.write("".join(format_session(session) + "\n" for session in sessions))
            if not sessions:
                out.write("No sessions.\n")

    asyncio.run(run())
    return 0


def build_parser() -> argparse.ArgumentParser:
    """``mertina [--env-file PATH] [chat|run|sessions|gateway|db] ...``."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file", type=Path, default=argparse.SUPPRESS, help="settings file to read"
    )
    parser = argparse.ArgumentParser(
        prog="mertina", description="Mertina developer CLI.", parents=[common]
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    chat = commands.add_parser(
        "chat", aliases=["run"], parents=[common], help="chat in a saved session (default)"
    )
    chat.add_argument("--resume", metavar="SESSION_ID", help="continue a saved session")
    sessions = commands.add_parser("sessions", parents=[common], help="list recent sessions")
    sessions.add_argument("--limit", type=int, default=20, help="how many to show")
    commands.add_parser("gateway", parents=[common], help="start the HTTP gateway")
    database = commands.add_parser("db", parents=[common], help="create or check the schema")
    database.add_argument("action", choices=("init", "check"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one ``mertina`` command and return its exit code."""
    args = build_parser().parse_args(argv)
    command = args.command or "chat"
    try:
        env_file = resolve_env_file(getattr(args, "env_file", None))
        env_argv = ["--env-file", str(env_file)] if env_file is not None else []
        if command == "gateway":
            from mertina_agent.gateway.__main__ import main as gateway_main

            return gateway_main(env_argv)
        if command == "db":
            from mertina_agent.state.__main__ import main as state_main

            return state_main([args.action, *env_argv])
        settings = load_settings(env_file=env_file)
        if command == "sessions":
            return _sessions(settings, args.limit, sys.stdout)
        return _chat(settings, getattr(args, "resume", None), sys.stdout)
    except MertinaError as exc:
        sys.stderr.write(f"mertina: {exc}\n")
        if "schema" in str(exc).lower():
            sys.stderr.write("Create it with: mertina db init\n")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
