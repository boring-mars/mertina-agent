"""Real Agent and SQLite persistence across two listener lifetimes; no hosted LLM."""

import asyncio
import copy

import pytest
from aiohttp import ClientSession
from pydantic import SecretStr

from mertina_agent.agent.core import Agent
from mertina_agent.agent.transports.types import NormalizedResponse, ToolCall
from mertina_agent.config import Settings
from mertina_agent.exceptions import (
    DatabaseError,
    ModelRequestError,
    RunNotFoundError,
    StorageError,
)
from mertina_agent.gateway.run import GatewayRunner
from mertina_agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


class Model:
    def __init__(self, script, *, block=False):
        self.script = list(script)
        self.requests = []
        self.entered = asyncio.Event()
        self.block = block
        self.closed = False

    async def complete(self, messages, *, tools=()):
        self.offered_tools = list(tools)
        self.requests.append(copy.deepcopy(list(messages)))
        value = self.script.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def stream(self, messages, *, tools=(), on_text_delta=None, on_tool_started=None):
        if self.block:
            self.requests.append(copy.deepcopy(list(messages)))
            if on_text_delta:
                on_text_delta("Partial answer")
            self.entered.set()
            await asyncio.Event().wait()
        response = await self.complete(messages, tools=tools)
        if on_text_delta and response.content:
            on_text_delta(response.content)
        if on_tool_started:
            for call in response.tool_calls:
                on_tool_started(call.name)
        return response

    async def aclose(self):
        self.closed = True


class OwnedAgent(Agent):
    def __init__(self, settings, model, callback):
        self.model = model
        registry = ToolRegistry()
        registry.register(
            "echo",
            "core",
            {"name": "echo", "parameters": {"type": "object"}},
            lambda arguments: str(arguments),
        )
        super().__init__(
            settings, model_client=model, tool_registry=registry, event_callback=callback
        )

    async def aclose(self):
        await super().aclose()
        await self.model.aclose()


@pytest.mark.parametrize("scenario", ["text", "tools", "refusal", "failed", "stopped"])
def test_committed_history_replayed_exactly_after_restart(tmp_path, scenario):
    asyncio.run(_restart_scenario(tmp_path, scenario))


async def _restart_scenario(tmp_path, scenario):
    settings = Settings(
        database_url=SecretStr("sqlite+aiosqlite:///" + str(tmp_path / "history.db")),
        gateway_port=0,
        gateway_bootstrap=True,
        llm_max_attempts=1,
    )
    script = [NormalizedResponse("First reply", (), "stop")]
    if scenario == "tools":
        script.insert(
            0, NormalizedResponse(None, (ToolCall("call_1", "echo", '{"x":"汉字"}'),), "tool_calls")
        )
    elif scenario == "refusal":
        script = [NormalizedResponse(None, (), "stop", refusal="Cannot answer")]
    elif scenario == "failed":
        script = [ModelRequestError("Unavailable", kind="connection")]
    first = Model(script, block=scenario == "stopped")
    async with (
        GatewayRunner(
            settings, agent_factory=lambda callback: OwnedAgent(settings, first, callback)
        ) as runner,
        ClientSession() as client,
    ):
        async with client.post(
            runner.address + "/api/sessions", json={"title": "恢复测试"}
        ) as response:
            assert response.status == 201
            session_id = (await response.json())["id"]
        run = await runner.registry.start_run(session_id, "first question")
        task = run.task
        if scenario == "stopped":
            await asyncio.wait_for(first.entered.wait(), 2)
            runner.registry.stop_run(run.id)
        await asyncio.wait_for(task, 3)
        assert run.durable
        assert run.status == (
            "stopped"
            if scenario == "stopped"
            else "failed"
            if scenario == "failed"
            else "completed"
        )
        snapshot = await runner.registry.store.load_history(session_id)
        saved = copy.deepcopy(snapshot.messages)
        assert len(saved) >= 2
        if scenario == "tools":
            assert saved[1]["tool_calls"][0]["function"]["arguments"] == '{"x":"汉字"}'
            assert saved[2]["tool_call_id"] == "call_1"
        elif scenario == "refusal":
            assert saved[-1]["refusal"] == "Cannot answer"
        elif scenario == "stopped":
            assert any("Partial answer" in (message.get("content") or "") for message in saved)
    assert first.closed

    second = Model([NormalizedResponse("Continued", (), "stop")])
    resumed_settings = settings.model_copy(
        update={"gateway_bootstrap": False, "llm_model": "new-model"}
    )
    async with GatewayRunner(
        resumed_settings,
        agent_factory=lambda callback: OwnedAgent(resumed_settings, second, callback),
    ) as runner:
        with pytest.raises(RunNotFoundError):
            runner.registry.get_run(run.id)
        assert (await runner.registry.store.load_history(session_id)).messages == saved
        continued = await runner.registry.start_run(session_id, "continue")
        await asyncio.wait_for(continued.task, 3)
        assert continued.durable
        request = second.requests[0]
        assert request[0]["role"] == "system"
        assert tuple(request[1:-1]) == saved
        assert request[-1] == {"role": "user", "content": "continue"}
        restored = await runner.registry.store.load_history(session_id)
        assert restored.messages[: len(saved)] == saved
        assert restored.session.history_revision == 2
        assert len(restored.messages) == len(saved) + 2
    assert second.closed


@pytest.mark.parametrize("written", [True, False])
def test_commit_receipt_reconciles_an_unconfirmed_turn_after_restart(tmp_path, written):
    asyncio.run(_reconciliation_scenario(tmp_path, written=written))


async def _reconciliation_scenario(tmp_path, *, written):
    settings = Settings(
        database_url=SecretStr("sqlite+aiosqlite:///" + str(tmp_path / "unconfirmed.db")),
        gateway_port=0,
        gateway_bootstrap=True,
        gateway_shutdown_timeout_s=0.2,
        gateway_cancel_timeout_s=0.2,
        llm_max_attempts=1,
    )
    first = Model([NormalizedResponse("Unconfirmed reply", (), "stop")])
    async with GatewayRunner(
        settings, agent_factory=lambda callback: OwnedAgent(settings, first, callback)
    ) as runner:
        store = runner.registry.store
        commit_turn = store.commit_turn

        async def lost_acknowledgement(command):
            # Either the write lands and its acknowledgement is lost, or the
            # connection fails first; reconciliation reads fail too.
            if written:
                await commit_turn(command)
            kind = "unknown"
            raise DatabaseError(kind)

        async def unavailable_receipt(_session_id, _operation_id):
            kind = "connection"
            raise DatabaseError(kind)

        store.commit_turn = lost_acknowledgement
        store.get_commit_receipt = unavailable_receipt
        async with ClientSession() as client:
            async with client.post(runner.address + "/api/sessions", json={}) as response:
                session_id = (await response.json())["id"]
            run = await runner.registry.start_run(session_id, "question")
            await asyncio.wait_for(run.task, 3)
            async with client.get(f"{runner.address}/v1/runs/{run.id}") as response:
                snapshot = await response.json()
        assert snapshot["error"] == "storage_unconfirmed"
        assert snapshot["reconciliation_required"] is True
        assert snapshot["durable"] is False

    resumed_settings = settings.model_copy(update={"gateway_bootstrap": False})
    async with (
        GatewayRunner(resumed_settings, agent_factory=lambda _callback: None) as runner,
        ClientSession() as client,
    ):
        async with client.get(f"{runner.address}/v1/runs/{run.id}") as response:
            assert response.status == 404
        path = f"{runner.address}/api/sessions/{session_id}/commits/{run.id}"
        async with client.get(path) as response:
            assert response.status == 200
            receipt = await response.json()
        if written:
            assert receipt == {
                "operation_id": run.id,
                "committed": True,
                "revision": 1,
                "first_seq": 1,
                "last_seq": 2,
                "status": "completed",
            }
        else:
            assert receipt == {"operation_id": run.id, "committed": False}


def test_missing_schema_fails_before_listening_and_can_be_initialized(tmp_path):
    asyncio.run(_startup_scenario(tmp_path))


async def _startup_scenario(tmp_path):
    settings = Settings(
        database_url=SecretStr("sqlite+aiosqlite:///" + str(tmp_path / "fresh.db")), gateway_port=0
    )
    runner = GatewayRunner(settings)
    with pytest.raises(StorageError):
        await runner.start()
    await runner.aclose()
    initialized = settings.model_copy(update={"gateway_bootstrap": True})
    async with GatewayRunner(initialized) as runner:
        assert runner.address.startswith("http://127.0.0.1:")
