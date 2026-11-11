"""Listener lifecycle and shutdown delivery with SQLite and offline agents."""

import asyncio
import json
import socket

import pytest
from aiohttp import ClientSession, ClientTimeout
from pydantic import SecretStr

from mertina_agent.agent.core import Agent
from mertina_agent.config import Settings
from mertina_agent.database.runtime import open_database
from mertina_agent.exceptions import ConfigurationError, DatabaseError
from mertina_agent.gateway.run import GatewayRunner
from mertina_agent.state.session_db import SessionDB
from mertina_agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


class WaitingModel:
    def __init__(self):
        self.started = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.close_gate = None
        self.closed = False

    async def complete(self, messages, *, tools=()):
        return await self.stream(messages, tools=tools)

    async def stream(self, messages, *, tools=(), on_text_delta=None, on_tool_started=None):
        del messages, tools, on_tool_started
        if on_text_delta is not None:
            on_text_delta("Partial shutdown answer")
        self.started.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.close_entered.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True


class OwnedAgent(Agent):
    def __init__(self, settings, model, callback):
        self.model = model
        super().__init__(
            settings, model_client=model, tool_registry=ToolRegistry(), event_callback=callback
        )

    async def aclose(self):
        await super().aclose()
        await self.model.aclose()


class UncooperativeAgent:
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False
        self.cancelled = False

    async def run_conversation(self, user_message, *, conversation_history=None):
        del user_message, conversation_history
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    def interrupt(self, reason=None):
        del reason
        return False

    async def aclose(self):
        self.closed = True


def local_settings(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        database_url=SecretStr("sqlite+aiosqlite:///" + str(tmp_path / "shutdown.sqlite3")),
        gateway_bootstrap=True,
        gateway_port=0,
        gateway_shutdown_timeout_s=0.1,
        gateway_cancel_timeout_s=0.2,
        **overrides,
    )


async def new_http_run(client, address):
    async with client.post(address + "/api/sessions", json={}) as response:
        assert response.status == 201
        session_id = (await response.json())["id"]
    async with client.post(
        address + "/v1/runs", json={"session_id": session_id, "message": "question"}
    ) as response:
        assert response.status == 202
        run_id = (await response.json())["run_id"]
    return session_id, run_id


def events_from_sse(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


@pytest.mark.parametrize("cooperative", [True, False])
def test_http_observer_receives_final_shutdown_status_and_resources_are_closed(
    tmp_path, cooperative
):
    async def scenario():
        settings = local_settings(tmp_path)
        model = WaitingModel()
        agent = UncooperativeAgent()
        factory = (
            (lambda callback: OwnedAgent(settings, model, callback))
            if cooperative
            else (lambda _callback: agent)
        )
        runner = GatewayRunner(settings, agent_factory=factory)
        try:
            await runner.start()
            address = runner.address
            database = runner.registry.store.database
            async with ClientSession(timeout=ClientTimeout(total=3)) as client:
                session_id, run_id = await new_http_run(client, address)
                await (model.started if cooperative else agent.started).wait()
                async with client.get(address + f"/v1/runs/{run_id}/events") as stream:
                    assert stream.status == 200
                    reading = asyncio.create_task(stream.text())
                    await runner.aclose()
                    events = events_from_sse(await reading)
                terminal = events[-1]
                assert terminal["type"] == ("run.stopped" if cooperative else "run.failed")
                assert terminal["data"]["durable"] is cooperative
                assert terminal["data"]["error"] == (None if cooperative else "shutdown_cancelled")
            assert model.closed if cooperative else agent.closed
            assert agent.cancelled is not cooperative
            with pytest.raises(ConfigurationError):
                _ = runner.address
            with pytest.raises(DatabaseError) as caught:
                await database.healthcheck()
            assert caught.value.kind == "closed"
            reopened = await open_database(settings)
            try:
                history = await SessionDB(reopened).load_history(session_id)
                assert history.session.history_revision == int(cooperative)
                if cooperative:
                    assert any(
                        "Partial shutdown answer" in (message.get("content") or "")
                        for message in history.messages
                    )
                else:
                    assert history.messages == ()
            finally:
                await reopened.aclose()
        finally:
            await runner.aclose()

    asyncio.run(bounded(scenario))


def test_bind_failure_unwinds_database_and_http_startup(tmp_path):
    async def scenario():
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        settings = local_settings(tmp_path).model_copy(update={"gateway_port": port})
        runner = GatewayRunner(settings)
        try:
            with pytest.raises(OSError, match=r"(?i)address already in use"):
                await runner.start()
            with pytest.raises(ConfigurationError):
                _ = runner.address
            with pytest.raises(DatabaseError) as caught:
                await runner.registry.store.database.healthcheck()
            assert caught.value.kind == "closed"
            await runner.aclose()
            with pytest.raises(ConfigurationError, match="only be started once"):
                await runner.start()
        finally:
            occupied.close()
            await runner.aclose()

    asyncio.run(bounded(scenario))


def test_cancelling_close_caller_does_not_cancel_owned_cleanup(tmp_path):
    async def scenario():
        settings = local_settings(tmp_path)
        model = WaitingModel()
        model.close_gate = asyncio.Event()
        runner = GatewayRunner(
            settings, agent_factory=lambda callback: OwnedAgent(settings, model, callback)
        )
        try:
            await runner.start()
            session = await runner.registry.store.create_session()
            run = await runner.registry.start_run(session.id, "question")
            await model.started.wait()
            closing = asyncio.create_task(runner.aclose())
            await model.close_entered.wait()
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert not model.closed
            model.close_gate.set()
            await runner.aclose()
            assert model.closed
            assert run.durable
            assert run.task is None
            assert runner.registry.active_run_id(session.id) is None
            with pytest.raises(DatabaseError) as caught:
                await runner.registry.store.database.healthcheck()
            assert caught.value.kind == "closed"
            owned_tasks = {
                task.get_name()
                for task in asyncio.all_tasks()
                if task.get_name().startswith(("gateway-", "run-")) and not task.done()
            }
            assert owned_tasks == set()
        finally:
            model.close_gate.set()
            await runner.aclose()

    asyncio.run(bounded(scenario))


async def bounded(scenario):
    async with asyncio.timeout(5):
        await scenario()
