"""Developer CLI chat against a real Agent and a temporary SQLite database; no hosted LLM."""

import asyncio
import io
import os
import signal

import pytest
from pydantic import SecretStr

from mertina_agent.agent.core import Agent
from mertina_agent.agent.transports.types import NormalizedResponse
from mertina_agent.cli import ChatSession, main, repl, run_turn
from mertina_agent.config import Settings
from mertina_agent.database.runtime import open_database
from mertina_agent.state.schema import Schema
from mertina_agent.state.session_db import SessionDB
from mertina_agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


class Model:
    """Answers each request with the next scripted reply; can block mid-stream."""

    def __init__(self, replies=(), *, block=False):
        self.replies = list(replies)
        self.block = block
        self.entered = asyncio.Event()
        self.requests = []

    async def complete(self, messages, *, tools=()):
        self.offered_tools = list(tools)
        self.requests.append(list(messages))
        return NormalizedResponse(self.replies.pop(0), (), "stop")

    async def stream(self, messages, *, tools=(), on_text_delta=None, on_tool_started=None):
        del on_tool_started  # this model never asks for tools
        if self.block:
            self.requests.append(list(messages))
            if on_text_delta:
                on_text_delta("Partial")
            self.entered.set()
            await asyncio.Event().wait()
        response = await self.complete(messages, tools=tools)
        if on_text_delta:
            on_text_delta(response.content)
        return response

    async def aclose(self):
        pass


def settings_for(tmp_path):
    return Settings(
        _env_file=None,
        database_url=SecretStr("sqlite+aiosqlite:///" + str(tmp_path / "cli.sqlite3")),
    )


def factory_for(settings, model):
    return lambda callback: Agent(
        settings, model_client=model, tool_registry=ToolRegistry(), event_callback=callback
    )


def script(*lines):
    """Feed lines to the REPL, then end input as Ctrl+D does."""
    remaining = iter(lines)

    def read_line(_prompt):
        try:
            return next(remaining)
        except StopIteration:
            raise EOFError from None

    return read_line


async def open_store(settings):
    database = await open_database(settings)
    schema = Schema()
    await schema.initialize(database, bootstrap=True)
    return database, SessionDB(database, schema=schema)


def test_turns_are_streamed_committed_and_resumed_by_a_new_chat(tmp_path):
    async def scenario():
        settings = settings_for(tmp_path)
        database, store = await open_store(settings)
        try:
            out = io.StringIO()
            chat = ChatSession(store, factory_for(settings, Model(["Hi!"])), out)
            assert await chat.send("Hello   there\n") == "completed"
            assert out.getvalue() == "Hi!\n"
            session = await store.get_session(chat.session_id)
            assert session.title == "Hello there"
            assert session.message_count == 2

            model = Model(["Still here."])
            resumed = ChatSession(store, factory_for(settings, model), io.StringIO())
            await resumed.resume(session.id)
            assert await resumed.send("Are you there?") == "completed"
            sent = [
                message["content"] for message in model.requests[0] if message["role"] != "system"
            ]
            assert sent == ["Hello   there\n", "Hi!", "Are you there?"]
            history = await store.load_history(session.id)
            assert [message["content"] for message in history.messages][-1] == "Still here."
        finally:
            await database.aclose()

    asyncio.run(scenario())


def test_ctrl_c_stops_the_reply_and_saves_the_partial_turn(tmp_path):
    async def scenario():
        settings = settings_for(tmp_path)
        database, store = await open_store(settings)
        try:
            out = io.StringIO()
            model = Model(block=True)
            chat = ChatSession(store, factory_for(settings, model), out)
            turn = asyncio.create_task(run_turn(chat, "Tell me a long story", out))
            await model.entered.wait()
            os.kill(os.getpid(), signal.SIGINT)
            await turn
            assert "[stopping…]" in out.getvalue()
            assert "[stopped:" in out.getvalue()
            history = await store.load_history(chat.session_id)
            assert history.messages[0]["content"] == "Tell me a long story"
            assert "Partial" in (history.messages[-1].get("content") or "")
        finally:
            await database.aclose()

    asyncio.run(scenario())


def test_abandoned_turn_saves_nothing_and_restores_ctrl_c(tmp_path):
    async def scenario():
        settings = settings_for(tmp_path)
        database, store = await open_store(settings)
        try:
            out = io.StringIO()
            model = Model(block=True)
            chat = ChatSession(store, factory_for(settings, model), out)
            chat.interrupt = lambda: False  # behave as if no agent can be stopped yet
            turn = asyncio.create_task(run_turn(chat, "Never mind", out))
            await model.entered.wait()
            os.kill(os.getpid(), signal.SIGINT)
            await turn
            assert "[turn abandoned; nothing was saved]" in out.getvalue()
            assert (await store.get_session(chat.session_id)).message_count == 0
        finally:
            await database.aclose()

    asyncio.run(scenario())
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_repl_handles_commands_and_messages_until_exit(tmp_path):
    settings = settings_for(tmp_path)
    out = io.StringIO()
    with asyncio.Runner() as runner:
        database, store = runner.run(open_store(settings))
        try:
            chat = ChatSession(store, factory_for(settings, Model(["One", "Two"])), out)
            lines = script("/help", "first", "/new", "", "second", "/sessions", "/bogus", "/exit")
            repl(runner, chat, out, read_line=lines)
            first_id = runner.run(store.list_sessions())[0].id
            chat.new()
            repl(runner, chat, out, read_line=script(f"/resume {first_id}", "/resume " + "0" * 32))
        finally:
            runner.run(database.aclose())
    text = out.getvalue()
    assert "/resume ID" in text
    assert "mertina> One\n" in text
    assert "mertina> Two\n" in text
    assert "Started a new conversation." in text
    assert text.count(" msgs  ") == 2
    assert "Unknown command" in text
    assert f"Resumed {first_id} (2 messages)." in text
    assert f"No session {'0' * 32}." in text
    assert chat.session_id == first_id


def test_main_reports_missing_schema_then_lists_sessions_after_db_init(
    tmp_path, monkeypatch, capsys
):
    env_file = tmp_path / "cli.env"
    env_file.write_text(f"MERTINA_DATABASE_URL=sqlite+aiosqlite:///{tmp_path / 'main.sqlite3'}\n")

    assert main(["sessions", "--env-file", str(env_file)]) == 1
    assert "mertina db init" in capsys.readouterr().err

    assert main(["db", "init", "--env-file", str(env_file)]) == 0
    assert main(["sessions", "--env-file", str(env_file)]) == 0
    assert "No sessions." in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", script())
    assert main(["--env-file", str(env_file)]) == 0
    assert "new conversation" in capsys.readouterr().out
