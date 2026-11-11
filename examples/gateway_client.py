"""HTTP-only helpers for the local Gateway examples; no model or database access."""

import argparse
import ipaddress
import json
import math
from collections.abc import AsyncIterator, Callable
from types import TracebackType
from typing import Self, cast
from urllib.parse import urlsplit

import aiohttp

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
TERMINAL_EVENTS = frozenset({"run.completed", "run.stopped", "run.failed", "run.partial"})


def local_url(value: str) -> str:
    """Accept only a plain HTTP loopback origin, without credentials or routing."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if (
            value != value.strip()
            or parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.port == 0
            or hostname is None
            or (hostname != "localhost" and not ipaddress.ip_address(hostname).is_loopback)
        ):
            raise ValueError
    except ValueError as exc:
        message = "Gateway URL must be an HTTP loopback origin without credentials"
        raise argparse.ArgumentTypeError(message) from exc
    return value.rstrip("/")


def positive_seconds(value: str) -> float:
    """Parse a finite positive CLI timeout."""
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError
    except ValueError as exc:
        message = "Expected a finite positive number of seconds"
        raise argparse.ArgumentTypeError(message) from exc
    return number


def nonnegative_seconds(value: str) -> float:
    """Parse a finite nonnegative delay, including immediate stop or reconnect."""
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError
    except ValueError as exc:
        message = "Expected a finite nonnegative number of seconds"
        raise argparse.ArgumentTypeError(message) from exc
    return number


def identifier(value: object) -> str:
    """Validate a server/session identity before constructing a request path."""
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(c not in "0123456789abcdef" for c in value)
    ):
        message = "Invalid Gateway identifier"
        raise ValueError(message)
    return value


def number(value: object) -> int:
    """Validate a cursor from the server without accepting booleans."""
    if type(value) is not int or value < 0:
        message = "Invalid Gateway event cursor"
        raise ValueError(message)
    return value


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        message = "Invalid Gateway response"
        raise ValueError(message)
    return cast(dict[str, object], value)


class GatewayClient:
    """Own a bounded aiohttp client that never follows redirects or proxy settings."""

    def __init__(self, base_url: str, *, timeout_s: float = 120) -> None:
        """Restrict requests to an explicitly selected local Gateway."""
        self.base_url = local_url(base_url)
        self.timeout_s = positive_seconds(str(timeout_s))
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Self:
        """Open the owned HTTP session."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout_s), trust_env=False
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release HTTP resources without interrupting any server-owned run."""
        if self._session is not None:
            await self._session.close()

    @property
    def http(self) -> aiohttp.ClientSession:
        """Return the context-owned client."""
        if self._session is None:
            message = "Gateway client is not open"
            raise RuntimeError(message)
        return self._session

    async def request(
        self, method: str, path: str, *, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Read one bounded JSON response and keep server error bodies private."""
        async with self.http.request(
            method, self.base_url + path, json=body, allow_redirects=False
        ) as response:
            if not 200 <= response.status < 300:
                message = f"Gateway request returned HTTP {response.status}"
                raise RuntimeError(message)
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    message = "Gateway response exceeds the example's bounded buffer"
                    raise RuntimeError(message)
            return _object(json.loads(data))

    async def ensure_session(self, session_id: str | None, *, title: str) -> str:
        """Create a conversation or confirm the requested existing conversation."""
        response = (
            await self.request("POST", "/api/sessions", body={"title": title})
            if session_id is None
            else await self.request("GET", "/api/sessions/" + identifier(session_id))
        )
        return identifier(response.get("id"))

    async def events(self, run_id: str, *, after: int = 0) -> AsyncIterator[dict[str, object]]:
        """Yield replay/live envelopes, or an explicit resynchronization notice."""
        path = "/v1/runs/" + identifier(run_id) + "/events"
        async with self.http.get(
            self.base_url + path,
            headers={"Last-Event-ID": str(number(after))},
            allow_redirects=False,
        ) as response:
            if response.status == 409:
                yield {"type": "resync_required"}
                return
            if response.status != 200:
                message = f"Gateway event stream returned HTTP {response.status}"
                raise RuntimeError(message)
            while not response.content.at_eof():
                frame = await response.content.readuntil(b"\n\n")
                for line in frame.splitlines():
                    if line.startswith(b"data: "):
                        event = _object(json.loads(line[6:]))
                        if event.get("code") == "resync_required":
                            event = {"type": "resync_required"}
                        yield event

    async def follow(
        self,
        run_id: str,
        *,
        after: int = 0,
        report: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        """Follow one run, recovering expired/overflowed cursors from its snapshot.

        A snapshot notification replaces transient display state. The example
        prints events as JSON; a UI should also clear text on retry.scheduled.
        """
        run_id = identifier(run_id)
        cursor = number(after)
        for _ in range(20):
            async for event in self.events(run_id, after=cursor):
                if event.get("type") == "resync_required":
                    break
                next_cursor = number(event.get("id"))
                if next_cursor <= cursor or event.get("run_id") != run_id:
                    message = "Gateway event ordering is inconsistent"
                    raise RuntimeError(message)
                cursor = next_cursor
                if report is not None:
                    report(event)
                if event.get("type") in TERMINAL_EVENTS:
                    return await self.request("GET", "/v1/runs/" + run_id)
            snapshot = await self.request("GET", "/v1/runs/" + run_id)
            cursor = number(snapshot.get("last_event_id"))
            if report is not None:
                report({"type": "snapshot", "data": snapshot})
            if snapshot.get("terminal") is True:
                return snapshot
        message = "Gateway stream repeatedly requires resynchronization"
        raise RuntimeError(message)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Register common HTTP example options."""
    parser.add_argument("prompt", nargs="?", default="Say hello in one sentence.")
    parser.add_argument("--base-url", type=local_url, default="http://127.0.0.1:8765")
    parser.add_argument(
        "--session-id", help="continue this existing session instead of creating one"
    )
    parser.add_argument("--title", default="Gateway example", help="title when creating a session")
    parser.add_argument(
        "--timeout", type=positive_seconds, default=120, help="total workflow seconds"
    )
