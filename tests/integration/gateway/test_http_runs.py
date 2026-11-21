import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import ClientTimeout
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from mertina_agent.agent.core import Agent
from mertina_agent.agent.transports.types import NormalizedResponse
from mertina_agent.config import Settings
from mertina_agent.database.runtime import open_database
from mertina_agent.gateway.platforms.api_server import create_app
from mertina_agent.gateway.runs import RunRegistry
from mertina_agent.state.schema import Schema
from mertina_agent.state.session_db import SessionDB
from mertina_agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


class PausedModel:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def complete(self, messages, *, tools=()):
        return await self.stream(messages, tools=tools)

    async def stream(self, messages, *, tools=(), on_text_delta=None, on_tool_started=None):
        del tools, on_tool_started
        self.requests.append(list(messages))
        if on_text_delta is not None:
            on_text_delta("Hello ")
        self.started.set()
        await self.release.wait()
        if on_text_delta is not None:
            on_text_delta("world")
        return NormalizedResponse(content="Hello world", tool_calls=(), finish_reason="stop")

    async def aclose(self):
        pass


@asynccontextmanager
async def serve(tmp_path, **overrides):
    settings = Settings(
        llm_model="test-model",
        database_url=SecretStr(f"sqlite+aiosqlite:///{tmp_path / 'http.sqlite3'}"),
        gateway_shutdown_timeout_s=0.2,
        gateway_cancel_timeout_s=0.2,
        **overrides,
    )
    model = PausedModel()
    database = await open_database(settings)
    schema = Schema()
    await schema.initialize(database, bootstrap=True)
    store = SessionDB(database, schema=schema)
    registry = RunRegistry(
        store,
        lambda callback: Agent(
            settings, model_client=model, tool_registry=ToolRegistry(), event_callback=callback
        ),
        settings,
    )
    app = create_app(registry, heartbeat_s=0.02, send_timeout_s=0.2, body_timeout_s=0.2)
    try:
        async with TestClient(TestServer(app), timeout=ClientTimeout(total=3)) as client:
            try:
                yield client, model, registry
            finally:
                await registry.aclose()
    finally:
        await database.aclose()
        await model.aclose()


async def create_session(client, title="new"):
    response = await client.post("/api/sessions", json={"title": title})
    assert response.status == 201
    return await response.json()


async def start_run(client, session_id, message="hello"):
    response = await client.post("/v1/runs", json={"session_id": session_id, "message": message})
    assert response.status == 202
    return await response.json()


async def next_event(response):
    async with asyncio.timeout(3):
        while True:
            frame = await response.content.readuntil(b"\n\n")
            assert frame, "SSE ended before the expected event"
            for line in frame.splitlines():
                if line.startswith(b"data: "):
                    return json.loads(line[6:])


async def until_terminal(response):
    events = []
    while True:
        event = await next_event(response)
        events.append(event)
        if event["type"] in {"run.completed", "run.stopped", "run.failed", "run.partial"}:
            return events


def test_sessions_crud_keyset_and_metadata_conflict(tmp_path):
    async def scenario():
        async with serve(tmp_path) as (client, _model, _registry):
            first = await create_session(client, "first")
            second = await create_session(client, "会话🦊")
            response = await client.get("/api/sessions", params={"limit": "1"})
            page = await response.json()
            assert [item["id"] for item in page["items"]] == [first["id"]]
            response = await client.get(
                "/api/sessions", params={"limit": "1", **page["next_cursor"]}
            )
            page = await response.json()
            assert [item["id"] for item in page["items"]] == [second["id"]]
            response = await client.patch(
                f"/api/sessions/{first['id']}", json={"title": "renamed", "expected_version": 0}
            )
            renamed = await response.json()
            assert renamed["metadata_version"] == 1
            assert renamed["history_revision"] == 0
            response = await client.patch(
                f"/api/sessions/{first['id']}", json={"title": "stale", "expected_version": 0}
            )
            assert response.status == 409
            response = await client.delete(f"/api/sessions/{first['id']}")
            assert response.status == 204
            response = await client.get(f"/api/sessions/{first['id']}")
            assert response.status == 404
            response = await client.get(f"/api/sessions/{second['id']}/messages")
            assert await response.json() == {"items": [], "through": 0, "next_after": None}

    asyncio.run(scenario())


def test_sse_disconnect_reconnect_broadcast_and_durable_messages(tmp_path):
    async def scenario():
        async with serve(tmp_path) as (client, model, _registry):
            session = await create_session(client)
            run = await start_run(client, session["id"])
            await model.started.wait()
            path = f"/v1/runs/{run['run_id']}"
            first = await client.get(path + "/events")
            first_events = [await next_event(first) for _ in range(3)]
            assert first_events[-1]["type"] == "text.delta"
            first.close()
            response = await client.get(path)
            snapshot = await response.json()
            assert snapshot["status"] == "running"
            assert snapshot["text"] == "Hello "
            response = await client.get(f"/api/sessions/{session['id']}")
            assert (await response.json())["active_run_id"] == run["run_id"]
            cursor = str(first_events[-1]["id"])
            resumed = await client.get(path + "/events", headers={"Last-Event-ID": cursor})
            broadcast = await client.get(path + "/events", headers={"Last-Event-ID": cursor})
            assert await resumed.content.readuntil(b"\n\n") == b": keepalive\n\n"
            model.release.set()
            resumed_events = await until_terminal(resumed)
            broadcast_events = await until_terminal(broadcast)
            assert resumed_events == broadcast_events
            assert resumed_events[0]["id"] == int(cursor) + 1
            assert resumed_events[-1]["data"]["durable"] is True
            response = await client.get(f"/api/sessions/{session['id']}/messages?limit=1")
            page = await response.json()
            assert page == {
                "items": [{"seq": 1, "message": {"role": "user", "content": "hello"}}],
                "through": 2,
                "next_after": 1,
            }
            response = await client.get(
                f"/api/sessions/{session['id']}/messages?after=1&through=2&limit=1"
            )
            page = await response.json()
            assert page["items"] == [
                {"seq": 2, "message": {"role": "assistant", "content": "Hello world"}}
            ]
            assert page["next_after"] is None
            response = await client.get(path)
            assert (await response.json())["durable"] is True
            replay = await client.get(path + "/events", headers={"Last-Event-ID": cursor})
            assert await until_terminal(replay) == resumed_events
            resumed.close()
            broadcast.close()
            replay.close()

    asyncio.run(scenario())


def test_session_busy_stop_is_idempotent_and_other_sessions_run(tmp_path):
    async def scenario():
        async with serve(tmp_path) as (client, model, _registry):
            first = await create_session(client)
            second = await create_session(client)
            run = await start_run(client, first["id"])
            await model.started.wait()
            response = await client.post(
                "/v1/runs", json={"session_id": first["id"], "message": "conflict"}
            )
            assert response.status == 409
            response = await client.delete(f"/api/sessions/{first['id']}")
            assert response.status == 409
            other_run = await start_run(client, second["id"])
            assert other_run["run_id"] != run["run_id"]
            stop_path = f"/v1/runs/{run['run_id']}/stop"
            response = await client.post(stop_path)
            assert (await response.json())["stop_requested"] is True
            response = await client.post(stop_path)
            assert response.status == 200
            stream = await client.get(f"/v1/runs/{run['run_id']}/events")
            model.release.set()
            events = await until_terminal(stream)
            assert events[-1]["type"] == "run.stopped"
            assert sum(event["type"] == "run.stop_requested" for event in events) == 1
            assert events[-1]["data"]["durable"] is True
            stream.close()

    asyncio.run(scenario())


def test_expired_and_future_sse_cursors_require_snapshot_resync(tmp_path):
    async def scenario():
        async with serve(tmp_path, gateway_event_capacity=2) as (client, model, _registry):
            session = await create_session(client)
            run = await start_run(client, session["id"])
            await model.started.wait()
            path = f"/v1/runs/{run['run_id']}"
            for cursor in ("0", "999"):
                response = await client.get(path + "/events", headers={"Last-Event-ID": cursor})
                assert response.status == 409
                assert (await response.json())["error"]["code"] == "resync_required"
            response = await client.get(path)
            snapshot = await response.json()
            stream = await client.get(
                path + "/events", headers={"Last-Event-ID": str(snapshot["last_event_id"])}
            )
            model.release.set()
            assert (await until_terminal(stream))[-1]["type"] == "run.completed"
            stream.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/api/sessions", {"title": "secret\x00"}, 400),
        ("/api/sessions", {"title": 123}, 400),
        ("/api/sessions", {"unexpected": "secret"}, 400),
        ("/api/sessions", [], 400),
        ("/v1/runs", {"session_id": "x" * 32, "message": "secret"}, 400),
        ("/v1/runs", {"session_id": "a" * 32, "message": "secret"}, 404),
    ],
)
def test_json_validation_is_strict_and_does_not_echo_input(tmp_path, path, body, status):
    async def scenario():
        async with serve(tmp_path) as (client, _model, _registry):
            response = await client.post(path, json=body)
            assert response.status == status
            assert "secret" not in await response.text()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        ('{"title":"one","title":"two"}', "application/json", 400),
        ('{"title":NaN}', "application/json", 400),
        ("{invalid", "application/json", 400),
        ('{"title":"one"}', "text/plain", 415),
        ("x" * 131073, "application/json", 413),
    ],
    ids=["duplicate-keys", "nonfinite", "malformed", "wrong-content-type", "too-large"],
)
def test_json_encoding_and_body_limits(tmp_path, body, content_type, status):
    async def scenario():
        async with serve(tmp_path) as (client, _model, _registry):
            response = await client.post(
                "/api/sessions", data=body, headers={"Content-Type": content_type}
            )
            assert response.status == status

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=101", "limit=1&limit=2", "limit=01", "after_id=" + "a" * 32, "extra=1"],
)
def test_session_query_validation(tmp_path, query):
    async def scenario():
        async with serve(tmp_path) as (client, _model, _registry):
            response = await client.get("/api/sessions?" + query)
            assert response.status == 400

    asyncio.run(scenario())


def test_message_cursor_validation_and_unknown_run(tmp_path):
    async def scenario():
        async with serve(tmp_path) as (client, _model, _registry):
            session = await create_session(client)
            for query in ("after=1", "through=1", "after=2&through=1", "after=-1", "limit=1.0"):
                response = await client.get(f"/api/sessions/{session['id']}/messages?{query}")
                assert response.status == 400
            for suffix in ("", "/events", "/stop"):
                method = client.post if suffix == "/stop" else client.get
                response = await method("/v1/runs/" + "a" * 32 + suffix)
                assert response.status == 404
            response = await client.get("/api/sessions/not-an-id")
            assert response.status == 400

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.example"},
        {"Host": "127.0.0.1:1"},
        {"Host": "127.0.0.1.attacker.example"},
        {"Host": "localhost@attacker.example"},
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1:1"},
        [("Origin", "http://localhost"), ("Origin", "http://localhost")],
    ],
)
def test_remote_or_ambiguous_authorities_cannot_access_local_api(tmp_path, headers):
    async def scenario():
        async with serve(tmp_path) as (client, _model, _registry):
            response = await client.post("/api/sessions", json={}, headers=headers)
            assert response.status == 403
            response = await client.get("/api/sessions")
            assert (await response.json())["items"] == []

    asyncio.run(scenario())


def test_same_origin_requests_and_localhost_authority_are_accepted(tmp_path):
    async def scenario():
        async with serve(tmp_path) as (client, _model, _registry):
            authority = f"localhost:{client.server.port}"
            response = await client.post(
                "/api/sessions",
                json={},
                headers={"Host": authority, "Origin": "http://" + authority},
            )
            assert response.status == 201
            response = await client.get(
                "/api/sessions", headers={"Host": authority, "Origin": "http://" + authority + "/"}
            )
            assert response.status == 403

    asyncio.run(scenario())


def test_admission_and_subscriber_limits_are_safe_http_failures(tmp_path):
    async def scenario():
        async with serve(tmp_path, gateway_max_active_runs=1, gateway_max_subscribers=1) as (
            client,
            model,
            _registry,
        ):
            session = await create_session(client)
            other = await create_session(client)
            run = await start_run(client, session["id"])
            await model.started.wait()
            response = await client.post(
                "/v1/runs", json={"session_id": other["id"], "message": "wait"}
            )
            assert response.status == 503
            path = f"/v1/runs/{run['run_id']}/events"
            stream = await client.get(path)
            response = await client.get(path)
            assert response.status == 503
            assert (await response.json())["error"]["code"] == "unavailable"
            for cursor in ("-1", "01", "1.0", ""):
                response = await client.get(path, headers={"Last-Event-ID": cursor})
                assert response.status == 400
            response = await client.get(
                path, headers=[("Last-Event-ID", "1"), ("Last-Event-ID", "2")]
            )
            assert response.status == 400
            model.release.set()
            assert (await until_terminal(stream))[-1]["type"] == "run.completed"
            stream.close()

    asyncio.run(scenario())


def test_message_high_water_mark_remains_fixed_after_another_turn(tmp_path):
    async def scenario():
        async with serve(tmp_path) as (client, model, _registry):
            session = await create_session(client)
            model.release.set()
            run = await start_run(client, session["id"])
            stream = await client.get(f"/v1/runs/{run['run_id']}/events")
            await until_terminal(stream)
            stream.close()
            path = f"/api/sessions/{session['id']}/messages"
            response = await client.get(path + "?limit=1")
            first_page = await response.json()
            assert first_page["through"] == 2
            run = await start_run(client, session["id"], "continue")
            stream = await client.get(f"/v1/runs/{run['run_id']}/events")
            await until_terminal(stream)
            stream.close()
            response = await client.get(path + "?after=1&through=2")
            stable_page = await response.json()
            assert len(stable_page["items"]) == 1
            assert stable_page["next_after"] is None
            response = await client.get(path)
            assert (await response.json())["through"] == 4

    asyncio.run(scenario())


def test_duplicate_host_header_is_rejected_before_mutation(tmp_path):
    async def scenario():
        async with serve(tmp_path) as (client, _model, _registry):
            reader, writer = await asyncio.open_connection("127.0.0.1", client.server.port)
            authority = f"127.0.0.1:{client.server.port}"
            request = (
                f"POST /api/sessions HTTP/1.1\r\nHost: {authority}\r\nHost: {authority}\r\n"
                "Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
            )
            try:
                writer.write(request.encode("ascii"))
                await writer.drain()
                async with asyncio.timeout(3):
                    status = await reader.readline()
                # aiohttp may reject duplicate Host before application middleware.
                assert status.split()[1] in {b"400", b"403"}
            finally:
                writer.close()
                await writer.wait_closed()
            response = await client.get("/api/sessions")
            assert (await response.json())["items"] == []

    asyncio.run(scenario())


def test_database_failure_is_a_safe_storage_error(tmp_path):
    async def scenario():
        async with serve(tmp_path) as (client, _model, registry):
            await registry.store.database.aclose()
            response = await client.get("/api/sessions")
            assert response.status == 503
            body = await response.json()
            assert body == {
                "error": {
                    "code": "storage_unavailable",
                    "message": "Session storage is unavailable or inconsistent",
                }
            }
            assert str(tmp_path) not in await response.text()

    asyncio.run(scenario())


def test_stalled_json_body_has_a_bounded_read_deadline(tmp_path):
    async def scenario():
        async with serve(tmp_path) as (client, _model, _registry):
            reader, writer = await asyncio.open_connection("127.0.0.1", client.server.port)
            request = (
                f"POST /api/sessions HTTP/1.1\r\nHost: 127.0.0.1:{client.server.port}\r\n"
                "Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n"
            )
            try:
                writer.write(request.encode("ascii"))
                await writer.drain()
                async with asyncio.timeout(3):
                    status = await reader.readline()
                assert status.split()[1] == b"408"
            finally:
                writer.close()
                await writer.wait_closed()
            response = await client.get("/api/sessions")
            assert (await response.json())["items"] == []

    asyncio.run(scenario())
