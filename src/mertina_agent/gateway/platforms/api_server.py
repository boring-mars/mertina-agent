"""Local Sessions API and request boundary for aiohttp.

Adapted from Hermes gateway/platforms/api_server.py responsibilities at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-gateway-sessions.md.
Database access uses the public SessionStore contract, with no handler SQL.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from aiohttp import web
from pydantic import ValidationError

from mertina_agent.exceptions import (
    DatabaseError,
    EventCursorError,
    EventSubscriptionError,
    GatewayClosingError,
    RunCapacityError,
    RunNotFoundError,
    SessionConflictError,
    SessionNotFoundError,
    StorageError,
)
from mertina_agent.gateway.api_models import (
    CreateSession,
    MessagePage,
    RenameSession,
    RequestModel,
    SessionPage,
    parse_unsigned,
    session_response,
    validate_identifier,
)
from mertina_agent.state.serialization import decode_message

if TYPE_CHECKING:
    from mertina_agent.gateway.runs import RunRegistry

logger = logging.getLogger(__name__)
BODY_TIMEOUT_KEY = web.AppKey("body_timeout_s", float)


def _authority(value: str, default_port: int) -> tuple[str, int]:
    if not value or any(character in value for character in "/\\@?#% \t\r\n"):
        raise web.HTTPForbidden()
    try:
        parsed = urlsplit("//" + value)
        hostname = parsed.hostname
        port = default_port if parsed.port is None else parsed.port
        if hostname != "localhost" and (
            hostname is None or not ipaddress.ip_address(hostname).is_loopback
        ):
            raise web.HTTPForbidden()
        if parsed.path or parsed.query or parsed.fragment or hostname is None:
            raise web.HTTPForbidden()
    except ValueError as exc:
        raise web.HTTPForbidden() from exc
    return hostname, port


def _check_local_request(request: web.Request) -> None:
    hosts = request.headers.getall("Host", [])
    origins = request.headers.getall("Origin", [])
    if len(hosts) != 1 or len(origins) > 1 or request.transport is None:
        raise web.HTTPForbidden()
    address = request.transport.get_extra_info("sockname")
    if not isinstance(address, tuple) or len(address) < 2:
        raise web.HTTPForbidden()
    default_port = 443 if request.secure else 80
    authority = _authority(hosts[0], default_port)
    if authority[1] != address[1]:
        raise web.HTTPForbidden()
    if origins:
        try:
            origin = urlsplit(origins[0])
            if (
                origins[0] != f"{origin.scheme}://{origin.netloc}"
                or origin.scheme != request.scheme
                or origin.path
                or origin.query
                or origin.fragment
                or _authority(origin.netloc, default_port) != authority
            ):
                raise web.HTTPForbidden()
        except ValueError as exc:
            raise web.HTTPForbidden() from exc


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


@web.middleware
async def _boundary(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    try:
        _check_local_request(request)
        return await handler(request)
    except (SessionNotFoundError, RunNotFoundError):
        return _error(404, "not_found", "The requested resource does not exist or has expired")
    except SessionConflictError:
        return _error(409, "conflict", "The session is busy or its version has changed")
    except EventCursorError:
        return _error(
            409, "resync_required", "Fetch the run snapshot and resume from last_event_id"
        )
    except (RunCapacityError, EventSubscriptionError, GatewayClosingError):
        return _error(503, "unavailable", "The Gateway cannot accept this operation now")
    except (DatabaseError, StorageError):
        return _error(503, "storage_unavailable", "Session storage is unavailable or inconsistent")
    except ValidationError:
        return _error(400, "invalid_request", "The request fields are invalid")
    except web.HTTPException as exc:
        codes = {
            400: "invalid_request",
            403: "forbidden_origin",
            404: "not_found",
            405: "method_not_allowed",
            408: "request_timeout",
            413: "request_too_large",
            415: "unsupported_media_type",
        }
        response = _error(exc.status, codes.get(exc.status, "request_failed"), exc.reason)
        if "Allow" in exc.headers:
            response.headers["Allow"] = exc.headers["Allow"]
        return response
    except Exception as exc:
        # Provider/driver exception chains can contain prompts and credentials.
        logger.error("HTTP request failed (%s)", type(exc).__name__)
        return _error(500, "internal_error", "The operation could not be completed")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise web.HTTPBadRequest()
        result[name] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise web.HTTPBadRequest()


async def read_body[T: RequestModel](request: web.Request, model: type[T]) -> T:
    """Read one strict JSON object without returning validation input or secrets."""
    if len(request.headers.getall("Content-Type", [])) != 1:
        raise web.HTTPBadRequest()
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        async with asyncio.timeout(request.app[BODY_TIMEOUT_KEY]):
            payload = await request.read()
        body = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (ValueError, UnicodeError) as exc:
        raise web.HTTPBadRequest() from exc
    except TimeoutError as exc:
        raise web.HTTPRequestTimeout() from exc
    return model.model_validate(body)


def read_query[T: RequestModel](
    request: web.Request, model: type[T], *, integers: frozenset[str]
) -> T:
    """Reject repeated query keys and parse numeric fields explicitly."""
    values: dict[str, object] = {}
    for name in request.query:
        if len(request.query.getall(name)) != 1:
            raise web.HTTPBadRequest()
        value = request.query[name]
        try:
            values[name] = parse_unsigned(value) if name in integers else value
        except ValueError as exc:
            raise web.HTTPBadRequest() from exc
    return model.model_validate(values)


def path_identifier(request: web.Request, name: str) -> str:
    """Validate an identifier extracted from the selected route."""
    try:
        return validate_identifier(request.match_info[name])
    except ValueError as exc:
        raise web.HTTPBadRequest() from exc


class SessionAPI:
    """Serve metadata and message operations through the domain store."""

    def __init__(self, registry: RunRegistry) -> None:
        """Borrow the runner-owned registry and storage lifetime."""
        self.registry = registry

    async def create(self, request: web.Request) -> web.Response:
        """Create a durable empty conversation."""
        body = await read_body(request, CreateSession)
        session = await self.registry.store.create_session(body.title)
        return web.json_response(session_response(session, active_run_id=None), status=201)

    async def list_sessions(self, request: web.Request) -> web.Response:
        """Return one ascending keyset page and a continuation cursor."""
        page = read_query(
            request, SessionPage, integers=frozenset({"limit", "after_created_at_us"})
        )
        after = (
            (page.after_created_at_us, page.after_id)
            if page.after_created_at_us is not None and page.after_id is not None
            else None
        )
        sessions = await self.registry.store.list_sessions(limit=page.limit, after=after)
        items = [
            session_response(session, active_run_id=self.registry.active_run_id(session.id))
            for session in sessions
        ]
        cursor = (
            {"after_created_at_us": sessions[-1].created_at_us, "after_id": sessions[-1].id}
            if len(sessions) == page.limit
            else None
        )
        return web.json_response({"items": items, "next_cursor": cursor})

    async def get(self, request: web.Request) -> web.Response:
        """Return durable metadata and discoverable active-run identity."""
        session_id = path_identifier(request, "session_id")
        session = await self.registry.store.get_session(session_id)
        return web.json_response(
            session_response(session, active_run_id=self.registry.active_run_id(session_id))
        )

    async def rename(self, request: web.Request) -> web.Response:
        """Compare and set title metadata independently from history revision."""
        session_id = path_identifier(request, "session_id")
        body = await read_body(request, RenameSession)
        session = await self.registry.store.rename_session(
            session_id, body.title, expected_version=body.expected_version
        )
        return web.json_response(
            session_response(session, active_run_id=self.registry.active_run_id(session_id))
        )

    async def delete(self, request: web.Request) -> web.Response:
        """Delete through the same session lease used by run admission."""
        await self.registry.delete_session(path_identifier(request, "session_id"))
        return web.Response(status=204)

    async def messages(self, request: web.Request) -> web.Response:
        """Page exact messages within a reusable committed high water mark."""
        session_id = path_identifier(request, "session_id")
        page = read_query(request, MessagePage, integers=frozenset({"after", "limit", "through"}))
        session = await self.registry.store.get_session(session_id)
        through = session.message_count if page.through is None else page.through
        if not page.after <= through <= session.message_count:
            raise web.HTTPBadRequest()
        payloads, end = await self.registry.store.read_messages(
            session_id, after=page.after, limit=page.limit, through=through
        )
        last = page.after + len(payloads)
        return web.json_response(
            {
                "items": [
                    {"seq": index, "message": decode_message(payload)}
                    for index, payload in enumerate(payloads, start=page.after + 1)
                ],
                "through": end,
                "next_after": last if last < end else None,
            }
        )


def create_app(
    registry: RunRegistry,
    *,
    heartbeat_s: float = 15,
    send_timeout_s: float = 10,
    body_timeout_s: float = 10,
) -> web.Application:
    """Create routes without owning registry, database or HTTP-server resources."""
    from mertina_agent.gateway.platforms.api_server_runs import RunsAPI

    if not math.isfinite(body_timeout_s) or body_timeout_s <= 0:
        message = "Request body timeout must be finite and positive"
        raise ValueError(message)
    app = web.Application(middlewares=[_boundary], client_max_size=131072)
    app[BODY_TIMEOUT_KEY] = body_timeout_s
    sessions = SessionAPI(registry)
    runs = RunsAPI(registry, heartbeat_s=heartbeat_s, send_timeout_s=send_timeout_s)
    app.add_routes(
        [
            web.post("/api/sessions", sessions.create),
            web.get("/api/sessions", sessions.list_sessions),
            web.get("/api/sessions/{session_id}", sessions.get),
            web.patch("/api/sessions/{session_id}", sessions.rename),
            web.delete("/api/sessions/{session_id}", sessions.delete),
            web.get("/api/sessions/{session_id}/messages", sessions.messages),
            web.post("/v1/runs", runs.create),
            web.get("/v1/runs/{run_id}", runs.get),
            web.get("/v1/runs/{run_id}/events", runs.events, allow_head=False),
            web.post("/v1/runs/{run_id}/stop", runs.stop),
        ]
    )
    return app
