"""Built Web UI served by the Gateway, with SPA fallback and the API boundary intact."""

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
from aiohttp import ClientTimeout
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from mertina_agent.config import Settings
from mertina_agent.database.runtime import open_database
from mertina_agent.gateway.platforms.api_server import create_app
from mertina_agent.gateway.platforms.static_site import BUILD_COMMAND, CONTENT_SECURITY_POLICY
from mertina_agent.gateway.run import GatewayRunner
from mertina_agent.gateway.runs import RunRegistry
from mertina_agent.state.schema import Schema
from mertina_agent.state.session_db import SessionDB

pytestmark = pytest.mark.integration

INDEX = "<!doctype html><title>Mertina</title><div id=root></div>"
SCRIPT = "console.log('mertina');"


def build(root):
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "app-1a2b.js").write_text(SCRIPT, encoding="utf-8")
    (root / ".env").write_text("MERTINA_LLM_API_KEY=secret", encoding="utf-8")
    return root


@asynccontextmanager
async def serve(tmp_path, static_dir):
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(f"sqlite+aiosqlite:///{tmp_path / 'static.sqlite3'}"),
    )
    database = await open_database(settings)
    schema = Schema()
    await schema.initialize(database, bootstrap=True)
    registry = RunRegistry(SessionDB(database, schema=schema), lambda _callback: None, settings)
    app = create_app(registry, static_dir=static_dir)
    try:
        async with TestClient(TestServer(app), timeout=ClientTimeout(total=3)) as client:
            yield client
    finally:
        await registry.aclose()
        await database.aclose()


def assert_static_headers(response):
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def assert_json_error(response, status, code):
    assert response.status == status
    assert response.content_type == "application/json"
    assert (await response.json())["error"]["code"] == code


@pytest.mark.parametrize("path", ["/", "/index.html", "/sessions/" + "a" * 32, "/settings"])
def test_entry_page_and_client_routes_serve_index(tmp_path, path):
    async def scenario():
        async with serve(tmp_path, build(tmp_path / "dist")) as client:
            response = await client.get(path)
            assert response.status == 200
            assert response.content_type == "text/html"
            assert await response.text() == INDEX
            assert response.headers["Cache-Control"] == "no-cache"
            assert_static_headers(response)

    asyncio.run(scenario())


def test_assets_are_served_with_their_type_and_security_headers(tmp_path):
    async def scenario():
        async with serve(tmp_path, build(tmp_path / "dist")) as client:
            response = await client.get("/assets/app-1a2b.js")
            assert response.status == 200
            assert response.content_type in {"text/javascript", "application/javascript"}
            assert await response.text() == SCRIPT
            assert "Cache-Control" not in response.headers
            assert_static_headers(response)
            response = await client.head("/assets/app-1a2b.js")
            assert response.status == 200

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path",
    [
        "/assets/missing-9f8e.js",
        "/.env",
        "/assets/%2e%2e/.env",
        "/%2e%2e/static.sqlite3",
        "/assets/..%5c..%5cstatic.sqlite3",
        "/escape/secret.txt",
    ],
)
def test_missing_assets_hidden_files_and_escapes_are_json_404(tmp_path, path):
    async def scenario():
        root = build(tmp_path / "dist")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        (root / "escape").symlink_to(outside, target_is_directory=True)
        async with serve(tmp_path, root) as client:
            response = await client.get(path)
            await assert_json_error(response, 404, "not_found")
            assert "secret" not in await response.text()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path", ["/api/unknown", "/api", "/v1/unknown", "/v1", "/api/sessions/x/y"]
)
def test_api_misses_keep_the_json_error_envelope(tmp_path, path):
    async def scenario():
        async with serve(tmp_path, build(tmp_path / "dist")) as client:
            await assert_json_error(await client.get(path), 404, "not_found")
            await assert_json_error(await client.post(path, json={}), 404, "not_found")

    asyncio.run(scenario())


def test_fallback_applies_only_to_get_and_api_routes_are_unchanged(tmp_path):
    async def scenario():
        async with serve(tmp_path, build(tmp_path / "dist")) as client:
            await assert_json_error(await client.post("/settings", json={}), 404, "not_found")
            await assert_json_error(await client.delete("/"), 404, "not_found")
            await assert_json_error(await client.put("/api/sessions"), 405, "method_not_allowed")
            response = await client.get("/api/sessions")
            assert response.status == 200
            assert await response.json() == {"items": [], "next_cursor": None}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "headers",
    [{"Host": "attacker.example"}, {"Origin": "https://attacker.example"}],
)
def test_static_requests_pass_the_local_request_boundary(tmp_path, headers):
    async def scenario():
        async with serve(tmp_path, build(tmp_path / "dist")) as client:
            for path in ("/", "/assets/app-1a2b.js", "/settings"):
                await assert_json_error(
                    await client.get(path, headers=headers), 403, "forbidden_origin"
                )

    asyncio.run(scenario())


def test_missing_build_logs_the_build_command_and_keeps_the_api(tmp_path, caplog):
    async def scenario():
        async with serve(tmp_path, tmp_path / "not-built") as client:
            await assert_json_error(await client.get("/"), 404, "not_found")
            response = await client.get("/api/sessions")
            assert response.status == 200

    caplog.set_level(logging.WARNING)
    asyncio.run(scenario())
    assert BUILD_COMMAND in caplog.text


def test_gateway_runner_serves_the_configured_directory(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            database_url=SecretStr(f"sqlite+aiosqlite:///{tmp_path / 'runner.sqlite3'}"),
            gateway_port=0,
            gateway_bootstrap=True,
            gateway_static_dir=build(tmp_path / "dist"),
        )
        async with GatewayRunner(settings, agent_factory=lambda _callback: None) as runner:
            port = runner.address.rsplit(":", 1)[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", int(port))
            try:
                writer.write(
                    f"GET /settings HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    "Connection: close\r\n\r\n".encode("ascii")
                )
                await writer.drain()
                async with asyncio.timeout(3):
                    payload = await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
            assert payload.startswith(b"HTTP/1.1 200")
            assert payload.endswith(INDEX.encode())

    asyncio.run(scenario())
