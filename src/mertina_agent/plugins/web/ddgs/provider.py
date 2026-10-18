"""DuckDuckGo search via the ``ddgs`` package (search only, no API key).

Copied from Hermes plugins/web/ddgs/provider.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md.

Isolation, kept from Hermes: ``ddgs`` and ``primp`` can block inside native code
while holding the GIL, so a thread-pool timeout could never fire and the whole
process would freeze. Each search therefore runs in a disposable child process
that the parent terminates, then kills, on timeout or interrupt. Hermes drives
the child from a helper thread; here it is an asyncio subprocess.
"""

import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

from mertina_agent.agent.interrupt import is_interrupted
from mertina_agent.agent.web_search_provider import SearchResponse, WebHit, WebSearchProvider
from mertina_agent.plugins.web._common import search_fail, search_ok, title_hit

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_TIMEOUT_S = 30.0
"""Hard wall-clock cap per search. ``DDGS(timeout=...)`` only bounds single HTTP
requests; its multi-engine retry loop has no overall cap."""

_POLL_INTERVAL_S = 0.1
_TERMINATE_GRACE_S = 1.0
_WORKER_MODULE = "mertina_agent.plugins.web.ddgs._search_worker"
# The child needs the network environment (proxies, certificates), not the
# agent's model credentials.
_STRIPPED_ENV_PREFIXES = ("MERTINA_", "OPENAI_")


class _SearchInterruptedError(Exception):
    """Raised when the run is asked to stop while a search is in flight."""


def _run_ddgs_search(query: str, safe_limit: int) -> list[WebHit]:
    """Run a blocking ddgs query and normalize its hits; executed in the child."""
    from ddgs import DDGS

    results: list[WebHit] = []
    with DDGS(timeout=10) as client:
        for index, hit in enumerate(client.text(query, max_results=safe_limit)):
            if index >= safe_limit:
                break
            results.append(
                title_hit(
                    str(hit.get("title", "")),
                    str(hit.get("href") or hit.get("url") or ""),
                    str(hit.get("body", "")),
                    index + 1,
                )
            )
    return results


def _child_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_STRIPPED_ENV_PREFIXES)
    }


def _parse_envelope(raw: bytes, returncode: int | None) -> list[WebHit]:
    """Decode the worker's stdout envelope; raise ``RuntimeError`` on any malformed shape."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        message = f"DDGS worker exited without a result (code={returncode})"
        raise RuntimeError(message)
    try:
        envelope: object = json.loads(text)
    except json.JSONDecodeError as exc:
        message = f"DDGS worker returned invalid JSON: {text[:200]!r}"
        raise RuntimeError(message) from exc
    if not isinstance(envelope, dict):
        message = f"DDGS worker returned an invalid envelope: {envelope!r}"
        raise RuntimeError(message)
    if not envelope.get("ok"):
        raise RuntimeError(str(envelope.get("error") or "DDGS worker failed"))
    results = envelope.get("results") or []
    if not isinstance(results, list) or not all(isinstance(row, dict) for row in results):
        message = "DDGS worker returned malformed results"
        raise RuntimeError(message)
    return [
        title_hit(
            str(row.get("title", "")),
            str(row.get("url", "")),
            str(row.get("description", "")),
            index + 1,
        )
        for index, row in enumerate(results)
    ]


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    """Terminate the worker, escalate to kill, and wait so no orphan remains."""
    for escalate in (process.terminate, process.kill):
        if process.returncode is not None:
            return
        try:
            escalate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), _TERMINATE_GRACE_S)
        except TimeoutError:
            continue
    if process.returncode is None:
        logger.warning("DDGS worker pid=%s did not exit after kill", process.pid)


async def _run_ddgs_search_bounded(
    query: str, safe_limit: int, *, timeout_s: float, command: Sequence[str]
) -> list[WebHit]:
    """Run a search in a disposable process with a hard deadline.

    The parent never waits on a child that may be stuck in native code: it polls
    the child and the interrupt signal and, on timeout or interrupt, kills it.

    Raises:
        TimeoutError: If the search outlives ``timeout_s``.
        _SearchInterruptedError: If the run is asked to stop.
        RuntimeError: If the worker reports a failure or a malformed result.
        OSError: If the worker cannot be started.
    """
    request = json.dumps({"query": query, "safe_limit": safe_limit}).encode("utf-8")
    # Own session / process group so terminate and kill also reach a hung
    # grandchild of the ddgs HTTP stack.
    spawn_options: dict[str, Any]
    if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
        spawn_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        spawn_options = {"start_new_session": True}
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        # A chatty child would deadlock a stdout-only drain.
        stderr=asyncio.subprocess.DEVNULL,
        env=_child_env(),
        **spawn_options,
    )
    communicate = asyncio.ensure_future(process.communicate(request))
    loop = asyncio.get_running_loop()
    interrupted = done = False
    raw = b""
    try:
        deadline = loop.time() + timeout_s
        while not (interrupted := is_interrupted()):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            finished, _ = await asyncio.wait(
                {communicate}, timeout=min(_POLL_INTERVAL_S, remaining)
            )
            if finished:
                raw, done = communicate.result()[0] or b"", True
                break
    finally:
        await _terminate_and_reap(process)
        if not communicate.done():
            communicate.cancel()
    if interrupted:
        message = "DuckDuckGo search interrupted"
        raise _SearchInterruptedError(message)
    if not done:
        message = f"DuckDuckGo search timed out after {timeout_s:g}s"
        raise TimeoutError(message)
    return _parse_envelope(raw, process.returncode)


class DDGSWebSearchProvider(WebSearchProvider):
    """DuckDuckGo search provider; failures are returned as results, never raised.

    DuckDuckGo rate-limits server-side, so failures are expected occasionally.
    """

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_SEARCH_TIMEOUT_S,
        worker_command: Sequence[str] | None = None,
    ) -> None:
        """Create a provider.

        Args:
            timeout_s: Hard wall-clock cap per search.
            worker_command: Command that serves one request on stdin/stdout;
                defaults to the bundled worker run by the current interpreter.
        """
        self._timeout_s = timeout_s
        self._command = (
            list(worker_command)
            if worker_command is not None
            else [sys.executable, "-m", _WORKER_MODULE]
        )

    @property
    def name(self) -> str:
        """The configuration value selecting this provider."""
        return "ddgs"

    def is_available(self) -> bool:
        """Return whether ``ddgs`` is installed, without importing it."""
        return importlib.util.find_spec("ddgs") is not None

    async def search(self, query: str, limit: int = 5) -> SearchResponse:
        """Run the search in a child process with a hard wall-clock timeout."""
        if not self.is_available():
            return search_fail("ddgs package is not installed")
        try:
            web_results = await _run_ddgs_search_bounded(
                query, max(1, limit), timeout_s=self._timeout_s, command=self._command
            )
        except TimeoutError:
            logger.warning("DDGS search timed out after %gs", self._timeout_s)
            return search_fail(
                f"DuckDuckGo search timed out after {self._timeout_s:g}s — DuckDuckGo may be "
                "rate-limiting or slow. Try again later."
            )
        except _SearchInterruptedError:
            logger.info("DDGS search interrupted")
            return search_fail("DuckDuckGo search interrupted")
        except (RuntimeError, OSError) as exc:
            logger.warning("DDGS search error: %s", exc)
            return search_fail(f"DuckDuckGo search failed: {exc}")
        logger.info("DDGS search: %d results (limit %d)", len(web_results), limit)
        return search_ok(web_results)
