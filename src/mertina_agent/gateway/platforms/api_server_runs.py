"""Runs HTTP API and reconnectable SSE subscribers.

Adapted from Hermes gateway/platforms/api_server_runs.py at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b, especially _handle_run_events.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt.
Disconnect cleanup releases only this subscriber, preserving run/replay state.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import TYPE_CHECKING

from aiohttp import web

from mertina_agent.gateway.api_models import StartRun, parse_unsigned
from mertina_agent.gateway.platforms.api_server import path_identifier, read_body

if TYPE_CHECKING:
    from mertina_agent.gateway.runs import RunRegistry


class RunsAPI:
    """Serve runner-owned tasks independently from request and stream lifetimes."""

    def __init__(self, registry: RunRegistry, *, heartbeat_s: float, send_timeout_s: float) -> None:
        """Require bounded heartbeats and network-write deadlines."""
        if any(not math.isfinite(value) or value <= 0 for value in (heartbeat_s, send_timeout_s)):
            message = "SSE timeouts must be finite and positive"
            raise ValueError(message)
        self.registry = registry
        self.heartbeat_s = heartbeat_s
        self.send_timeout_s = send_timeout_s

    async def create(self, request: web.Request) -> web.Response:
        """Admit a background turn and return its initial snapshot."""
        body = await read_body(request, StartRun)
        run = await self.registry.start_run(body.session_id, body.message)
        return web.json_response(run.snapshot(), status=202)

    async def get(self, request: web.Request) -> web.Response:
        """Return enough accumulated state to resynchronize an expired stream."""
        run = self.registry.get_run(path_identifier(request, "run_id"))
        return web.json_response(run.snapshot())

    async def stop(self, request: web.Request) -> web.Response:
        """Request idempotent cooperative interruption, including before startup."""
        run = self.registry.stop_run(path_identifier(request, "run_id"))
        return web.json_response(run.snapshot())

    async def _write(self, response: web.StreamResponse, data: bytes) -> None:
        async with asyncio.timeout(self.send_timeout_s):
            await response.write(data)

    async def events(self, request: web.Request) -> web.StreamResponse:
        """Replay then broadcast events; disconnection never stops the background run."""
        run = self.registry.get_run(path_identifier(request, "run_id"))
        cursors = request.headers.getall("Last-Event-ID", [])
        if len(cursors) > 1:
            raise web.HTTPBadRequest()
        try:
            after = parse_unsigned(cursors[0]) if cursors else 0
        except ValueError as exc:
            raise web.HTTPBadRequest() from exc
        # Subscribe before preparing headers so stale cursors remain a JSON 409.
        subscription = run.events.subscribe(after)
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        try:
            async with asyncio.timeout(self.send_timeout_s):
                await response.prepare(request)
            while True:
                try:
                    async with asyncio.timeout(self.heartbeat_s):
                        event = await subscription.queue.get()
                except TimeoutError:
                    await self._write(response, b": keepalive\n\n")
                    continue
                if event is None:
                    if subscription.resync_required:
                        await self._write(
                            response,
                            b'event: resync_required\ndata: {"code":"resync_required"}\n\n',
                        )
                    break
                payload = json.dumps(event.to_dict(), ensure_ascii=True, separators=(",", ":"))
                frame = f"id: {event.id}\nevent: {event.type}\ndata: {payload}\n\n"
                await self._write(response, frame.encode("utf-8"))
            async with asyncio.timeout(self.send_timeout_s):
                await response.write_eof()
        except (ConnectionError, TimeoutError):
            # A slow/disconnected client owns only its subscription. Closing the
            # transport also prevents an unfinished response from being reused.
            if request.transport is not None:
                request.transport.close()
        finally:
            subscription.close()
        return response
