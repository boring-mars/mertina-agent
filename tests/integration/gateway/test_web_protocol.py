"""Gateway protocol additions for the Web UI, over HTTP with a real Agent and SQLite."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import ClientTimeout
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from mertina_agent.agent.core import Agent
from mertina_agent.agent.transports.types import NormalizedResponse, ToolCall
from mertina_agent.config import Settings
from mertina_agent.database.runtime import open_database
from mertina_agent.gateway.platforms.api_server import create_app
from mertina_agent.gateway.runs import RunRegistry
from mertina_agent.state.schema import Schema
from mertina_agent.state.session_db import SessionDB
from mertina_agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

TERMINALS = {"run.completed", "run.stopped", "run.failed", "run.partial"}


class Step:
    """One scripted model response, optionally streamed in chunks and paused."""

    def __init__(self, response, *, chunks=None, gate=None):
        self.response = response
        self.chunks = chunks if chunks is not None else [response.content or ""]
        self.gate = gate
        self.streamed = asyncio.Event()


class ScriptedModel:
    def __init__(self, steps):
        self.steps = list(steps)

    async def complete(self, messages, *, tools=()):
        return await self.stream(messages, tools=tools)

    async def stream(self, messages, *, tools=(), on_text_delta=None, on_tool_started=None):
        del messages, tools
        step = self.steps.pop(0)
        for chunk in step.chunks:
            if on_text_delta is not None and chunk:
                on_text_delta(chunk)
        if on_tool_started is not None:
            for call in step.response.tool_calls:
                on_tool_started(call.name)
        step.streamed.set()
        if step.gate is not None:
            await step.gate.wait()
        return step.response

    async def aclose(self):
        pass


def echo_registry():
    registry = ToolRegistry()
    registry.register(
        "echo",
        "core",
        {"name": "echo", "parameters": {"type": "object"}},
        lambda arguments: json.dumps({"echo": arguments}),
    )
    return registry


@asynccontextmanager
async def serve(tmp_path, steps, **overrides):
    settings = Settings(
        _env_file=None,
        llm_model="test-model",
        llm_max_attempts=1,
        database_url=SecretStr(f"sqlite+aiosqlite:///{tmp_path / 'web.sqlite3'}"),
        gateway_shutdown_timeout_s=0.2,
        gateway_cancel_timeout_s=0.2,
        **overrides,
    )
    model = ScriptedModel(steps)
    database = await open_database(settings)
    schema = Schema()
    await schema.initialize(database, bootstrap=True)
    store = SessionDB(database, schema=schema)
    registry = RunRegistry(
        store,
        lambda callback: Agent(
            settings, model_client=model, tool_registry=echo_registry(), event_callback=callback
        ),
        settings,
    )
    app = create_app(registry, heartbeat_s=0.02, send_timeout_s=0.2, body_timeout_s=0.2)
    try:
        async with TestClient(TestServer(app), timeout=ClientTimeout(total=5)) as client:
            try:
                yield client, registry
            finally:
                await registry.aclose()
    finally:
        await database.aclose()


async def create_session(client, title="web"):
    response = await client.post("/api/sessions", json={"title": title})
    assert response.status == 201
    return await response.json()


async def start_run(client, session_id, message="hello"):
    response = await client.post("/v1/runs", json={"session_id": session_id, "message": message})
    assert response.status == 202
    return await response.json()


async def next_frame(response):
    """Return the next ``(id, event)`` SSE frame, skipping heartbeats."""
    async with asyncio.timeout(3):
        while True:
            frame = await response.content.readuntil(b"\n\n")
            assert frame, "SSE ended before the expected event"
            identifier = None
            for line in frame.splitlines():
                if line.startswith(b"id: "):
                    identifier = line[4:].decode()
                if line.startswith(b"data: "):
                    return identifier, json.loads(line[6:])


async def events_until(response, predicate):
    events = []
    while True:
        identifier, event = await next_frame(response)
        events.append((identifier, event))
        if predicate(event):
            return events


def tool_calls(*calls):
    return NormalizedResponse(
        None,
        tuple(ToolCall(call_id, "echo", json.dumps(args)) for call_id, args in calls),
        "tool_calls",
    )


def test_tool_arguments_and_terminal_fields_reach_sse_and_snapshot(tmp_path):
    long_value = "x" * 5000
    steps = [
        Step(
            tool_calls(("call_a", {"query": "asyncpg 3.13t wheels"}), ("call_b", {"v": long_value}))
        ),
        Step(NormalizedResponse("Done.", (), "stop")),
    ]

    async def scenario():
        async with serve(tmp_path, steps) as (client, _registry):
            session = await create_session(client)
            run = await start_run(client, session["id"])
            assert run["accepted_at_us"] > 0
            assert run["finished_at_us"] is None
            assert run["tool_calls"] == []
            stream = await client.get(f"/v1/runs/{run['run_id']}/events")
            events = [
                event for _, event in await events_until(stream, lambda e: e["type"] in TERMINALS)
            ]
            stream.close()
            started = [event["data"] for event in events if event["type"] == "tool.started"]
            assert started == [
                {
                    "call_id": "call_a",
                    "name": "echo",
                    "arguments": {"query": "asyncpg 3.13t wheels"},
                    "arguments_truncated": False,
                },
                {
                    "call_id": "call_b",
                    "name": "echo",
                    "arguments": None,
                    "arguments_truncated": True,
                },
            ]
            terminal = events[-1]
            assert terminal["type"] == "run.completed"
            response = await client.get(f"/v1/runs/{run['run_id']}")
            snapshot = await response.json()
            assert terminal["data"]["text_truncated"] is snapshot["text_truncated"] is False
            assert terminal["data"]["finished_at_us"] == snapshot["finished_at_us"]
            assert snapshot["finished_at_us"] >= snapshot["accepted_at_us"] == run["accepted_at_us"]
            assert [
                (call["call_id"], call["status"], call["arguments"])
                for call in snapshot["tool_calls"]
            ] == [
                ("call_a", "ok", {"query": "asyncpg 3.13t wheels"}),
                ("call_b", "ok", None),
            ]
            assert all(call["duration_s"] >= 0 for call in snapshot["tool_calls"])
            # The durable history still holds the complete, unbounded arguments.
            response = await client.get(f"/api/sessions/{session['id']}/messages")
            messages = [item["message"] for item in (await response.json())["items"]]
            arguments = messages[1]["tool_calls"][1]["function"]["arguments"]
            assert json.loads(arguments) == {"v": long_value}
            response = await client.post(f"/v1/runs/{run['run_id']}/stop")
            assert (await response.json())["finished_at_us"] == snapshot["finished_at_us"]

    asyncio.run(scenario())


def test_snapshot_rebuilds_tool_cards_after_the_event_buffer_overflows(tmp_path):
    gate = asyncio.Event()
    steps = [
        Step(tool_calls(("call_a", {"query": "one"}), ("call_b", {"query": "two"}))),
        Step(
            NormalizedResponse("t" * 60, (), "stop"),
            chunks=["t"] * 60,
            gate=gate,
        ),
    ]

    async def scenario():
        async with serve(tmp_path, steps, gateway_event_capacity=16) as (client, _registry):
            session = await create_session(client)
            run = await start_run(client, session["id"])
            path = f"/v1/runs/{run['run_id']}"
            stream = await client.get(path + "/events")
            seen = await events_until(
                stream,
                lambda e: e["type"] == "tool.finished" and e["data"]["call_id"] == "call_b",
            )
            stream.close()
            cursor = seen[-1][0]
            started = {
                event["data"]["call_id"]: event["data"]
                for _, event in seen
                if event["type"] == "tool.started"
            }
            await asyncio.wait_for(steps[1].streamed.wait(), 3)
            response = await client.get(path + "/events", headers={"Last-Event-ID": cursor})
            assert response.status == 409
            assert (await response.json())["error"]["code"] == "resync_required"
            response = await client.get(path)
            snapshot = await response.json()
            assert snapshot["text"] == "t" * 60
            assert [call["call_id"] for call in snapshot["tool_calls"]] == ["call_a", "call_b"]
            for call in snapshot["tool_calls"]:
                assert call["status"] == "ok"
                assert call["arguments"] == started[call["call_id"]]["arguments"]
                assert call["arguments_truncated"] is False
            resumed = await client.get(
                path + "/events", headers={"Last-Event-ID": str(snapshot["last_event_id"])}
            )
            gate.set()
            rest = await events_until(resumed, lambda e: e["type"] in TERMINALS)
            resumed.close()
            assert rest[-1][1]["type"] == "run.completed"

    asyncio.run(scenario())
