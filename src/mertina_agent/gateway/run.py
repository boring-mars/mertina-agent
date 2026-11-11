"""Assemble and own the local Gateway's resources in lifecycle order.

Adapted from Hermes GatewayRunner startup/drain/stop responsibilities at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-gateway-sessions.md.
"""

import asyncio
from contextlib import suppress
from types import TracebackType
from typing import Self

from aiohttp import web
from pydantic import ValidationError

from mertina_agent.config import Settings
from mertina_agent.database.runtime import Database, open_database
from mertina_agent.exceptions import ConfigurationError
from mertina_agent.gateway.platforms.api_server import create_app
from mertina_agent.gateway.runs import RunRegistry
from mertina_agent.gateway.session import AgentFactory, build_agent_factory
from mertina_agent.state.schema import Schema
from mertina_agent.state.session_db import SessionDB


class GatewayRunner:
    """Own one listener, database, run registry and periodic retention task."""

    def __init__(self, settings: Settings, *, agent_factory: AgentFactory | None = None) -> None:
        """Accept validated settings and an optional offline agent factory for tests."""
        try:
            self.settings = Settings.model_validate(settings.model_dump())
        except ValidationError as exc:
            message = "Invalid Gateway configuration"
            raise ConfigurationError(message) from exc
        self._factory = agent_factory
        self._database: Database | None = None
        self._registry: RunRegistry | None = None
        self._http: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._reaper: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._address: str | None = None
        self._started = False

    @property
    def registry(self) -> RunRegistry:
        """Expose the configured registry after successful startup."""
        if self._registry is None:
            message = "Gateway has not started"
            raise ConfigurationError(message)
        return self._registry

    @property
    def address(self) -> str:
        """Return the actual listening URL, including an OS-selected ephemeral port."""
        if self._address is None:
            message = "Gateway is not listening"
            raise ConfigurationError(message)
        return self._address

    async def start(self) -> None:
        """Validate durable schema before accepting HTTP traffic; unwind failed startup."""
        if self._started or self._close_task is not None:
            message = "Gateway runner instances can only be started once"
            raise ConfigurationError(message)
        self._started = True
        try:
            self._database = await open_database(self.settings)
            schema = Schema()
            await schema.initialize(self._database, bootstrap=self.settings.gateway_bootstrap)
            self._registry = RunRegistry(
                SessionDB(self._database, schema=schema),
                self._factory if self._factory is not None else build_agent_factory(self.settings),
                self.settings,
            )
            app = create_app(
                self._registry,
                heartbeat_s=self.settings.gateway_heartbeat_s,
                send_timeout_s=self.settings.gateway_send_timeout_s,
                body_timeout_s=self.settings.gateway_body_timeout_s,
            )
            self._http = web.AppRunner(
                app, access_log=None, shutdown_timeout=self.settings.gateway_cancel_timeout_s
            )
            await self._http.setup()
            self._site = web.TCPSite(
                self._http,
                host=self.settings.gateway_host,
                port=self.settings.gateway_port,
                reuse_port=False,
            )
            await self._site.start()
            port = self._http.addresses[0][1]
            host = self.settings.gateway_host
            authority = f"[{host}]" if ":" in host else host
            self._address = f"http://{authority}:{port}"
            self._reaper = asyncio.create_task(self._reap(), name="gateway-retention")
        except BaseException:
            await self.aclose()
            raise

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(min(60.0, self.settings.gateway_run_ttl_s))
            self.registry.prune()

    async def _close(self) -> None:
        # Stop the listener first; existing SSE clients can still receive the
        # terminal events published by cooperative shutdown and durable commits.
        if self._site is not None:
            await self._site.stop()
        if self._reaper is not None:
            self._reaper.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper
        try:
            if self._registry is not None:
                await self._registry.aclose()
        finally:
            try:
                if self._http is not None:
                    await self._http.cleanup()
            finally:
                if self._database is not None:
                    async with asyncio.timeout(
                        self.settings.database_timeout_s + self.settings.gateway_cancel_timeout_s
                    ):
                        await self._database.aclose()
                self._address = None

    async def aclose(self) -> None:
        """Idempotently drain runs, close HTTP and dispose the owned database pool."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="gateway-close")
        await asyncio.shield(self._close_task)

    async def __aenter__(self) -> Self:
        """Start a listener for the duration of the context."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Finish all owned lifecycle phases on exit."""
        await self.aclose()
