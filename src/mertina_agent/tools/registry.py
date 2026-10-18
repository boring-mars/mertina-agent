"""Central tool registry: schemas, handlers, availability checks and dispatch.

Adapted from Hermes tools/registry.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md for the pinned source and reductions.

As in Hermes, each tool module calls ``registry.register()`` at import time and
the loop queries the registry instead of keeping parallel data structures. The
plugin overlays, discovery scan, availability cache and MCP support are left
out. Dispatch is asynchronous: synchronous handlers run in a worker thread so a
slow tool never blocks the event loop.
"""

import asyncio
import copy
import inspect
import json
import logging
import re
import threading
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import cast

from mertina_agent.agent.transports import ChatCompletionsTransport
from mertina_agent.agent.transports.types import (
    FunctionDefinition,
    JsonObject,
    JsonValue,
    ToolDefinition,
)
from mertina_agent.exceptions import ModelInputError, ToolRegistrationError

logger = logging.getLogger(__name__)

type SyncToolHandler = Callable[[JsonObject], str]
type AsyncToolHandler = Callable[[JsonObject], Awaitable[str]]
type ToolHandler = SyncToolHandler | AsyncToolHandler
type AvailabilityCheck = Callable[[], bool]

# Cap on a tool error body: it trims runaway interpolated exceptions, which would
# otherwise stack up in context across retries. Static messages are far shorter.
_MAX_TOOL_ERROR_CHARS = 2048
_TOOL_ERROR_TRUNCATION_MARKER = "… [truncated]"
# Logs keep more of an error body than the model sees, but still a bounded amount.
_MAX_LOGGED_ERROR_CHARS = 8192

# Chat Completions providers reject any other function name, and they reject it
# with a 400 for the whole turn. Failing at registration names the culprit.
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Framing tokens stripped from exception text before the model reads it, so a
# tool error cannot pose as a role boundary, a tool call or a code fence.
_TOOL_ERROR_STRIP_RES = (
    re.compile(
        r"</?(?:tool_call|function_call|result|response|output|input|system|assistant|user)>",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*```(?:json|xml|html|markdown)?\s*", re.MULTILINE),
    re.compile(r"\s*```\s*$", re.MULTILINE),
    re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL),
)


def _bound_error_text(text: str) -> str:
    """Bound an error body destined for model context; logs keep a longer prefix."""
    if len(text) <= _MAX_TOOL_ERROR_CHARS:
        return text
    logger.debug(
        "Tool error body truncated for context (%d chars): %s",
        len(text),
        text[:_MAX_LOGGED_ERROR_CHARS],
    )
    return text[:_MAX_TOOL_ERROR_CHARS] + _TOOL_ERROR_TRUNCATION_MARKER


def _bound_json_error_result(result: str) -> str:
    """Trim an oversized ``error`` field in a JSON string result.

    Handlers that serialize ``{"error": str(exc)}`` themselves bypass the cap in
    :func:`tool_error`, so it is enforced again at the dispatch boundary.
    """
    if len(result) <= _MAX_TOOL_ERROR_CHARS or '"error"' not in result:
        return result
    try:
        payload: object = json.loads(result)
    except ValueError:
        return result
    if not isinstance(payload, dict):
        return result
    error = payload.get("error")
    if not isinstance(error, str) or len(error) <= _MAX_TOOL_ERROR_CHARS:
        return result
    payload["error"] = _bound_error_text(error)
    return json.dumps(payload, ensure_ascii=False)


def _sanitize_tool_error(error_message: str) -> str:
    """Strip structural framing tokens from a tool error and cap its length.

    Hermes keeps this helper in ``model_tools.py`` and imports it lazily from the
    registry. Here it lives in the lower layer, which removes the import cycle.
    """
    sanitized = error_message
    for pattern in _TOOL_ERROR_STRIP_RES:
        sanitized = pattern.sub("", sanitized)
    if len(sanitized) > _MAX_TOOL_ERROR_CHARS:
        sanitized = sanitized[: _MAX_TOOL_ERROR_CHARS - 3] + "..."
    return f"[TOOL_ERROR] {sanitized}"


def tool_error(message: object, **extra: JsonValue) -> str:
    """Serialize a tool failure as ``{"error": <message>, **extra}``.

    The error body is bounded so a raw exception cannot bloat the history.
    """
    payload: JsonObject = {"error": _bound_error_text(str(message)), **extra}
    return json.dumps(payload, ensure_ascii=False)


def tool_result(data: JsonObject | None = None, **fields: JsonValue) -> str:
    """Serialize a successful tool result from a dict or from keyword fields.

    Raises:
        TypeError: If both ``data`` and keyword fields are given.
    """
    if data is not None and fields:
        message = "Pass either a data dict or keyword fields, not both"
        raise TypeError(message)
    return json.dumps(data if data is not None else fields, ensure_ascii=False)


@dataclass(frozen=True)
class ToolEntry:
    """Everything the registry knows about one tool.

    Attributes:
        name: Function name the model uses to call the tool.
        toolset: Group the tool is enabled through.
        definition: Validated function declaration sent to the model.
        handler: Callable that runs the tool with its parsed arguments.
        check_fn: Availability probe; a tool whose probe fails is not offered.
        is_async: Whether ``handler`` returns an awaitable.
        description: Human-readable summary, defaulting to the schema's.
    """

    name: str
    toolset: str
    definition: ToolDefinition
    handler: ToolHandler
    check_fn: AvailabilityCheck | None
    is_async: bool
    description: str


def _run_check_fn(check_fn: AvailabilityCheck) -> bool:
    """Run an availability probe, treating any failure as "unavailable"."""
    try:
        return bool(check_fn())
    except Exception:
        # A broken probe must hide its tools, not break the tool listing.
        logger.warning("Tool availability check %r raised", check_fn, exc_info=True)
        return False


class ToolRegistry:
    """Collect tool declarations and dispatch calls to their handlers.

    Registration is serialized by a lock and reads work on snapshots, so tools
    may be registered from any thread while calls are being dispatched.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._tools: dict[str, ToolEntry] = {}
        self._lock = threading.Lock()
        self._transport = ChatCompletionsTransport()

    def register(
        self,
        name: str,
        toolset: str,
        schema: FunctionDefinition,
        handler: ToolHandler,
        *,
        check_fn: AvailabilityCheck | None = None,
        is_async: bool = False,
        description: str = "",
    ) -> None:
        """Declare a tool.

        Re-registering a name within the same toolset replaces the earlier
        declaration. The schema is validated and copied, so later changes to
        the caller's dict do not reach the model.

        Args:
            name: Function name, 1-64 characters of letters, digits, ``_`` or ``-``.
            toolset: Non-empty group name used to enable the tool.
            schema: Function declaration; its ``name`` must equal ``name``.
            handler: Receives the parsed arguments and returns a string result.
            check_fn: Optional availability probe, run whenever tools are listed.
            is_async: Set when ``handler`` returns an awaitable.
            description: Summary for humans; defaults to the schema description.

        Raises:
            ToolRegistrationError: If the declaration is malformed or ``name``
                is already registered by another toolset.
        """
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            message = f"Tool name {name!r} must be 1-64 letters, digits, '_' or '-'"
            raise ToolRegistrationError(message)
        if not isinstance(toolset, str) or not toolset.strip():
            message = f"Tool {name!r}: toolset must be a non-empty string"
            raise ToolRegistrationError(message)
        if inspect.iscoroutinefunction(handler) and not is_async:
            # Dispatching it as synchronous would leak an un-awaited coroutine.
            message = f"Tool {name!r}: coroutine handler requires is_async=True"
            raise ToolRegistrationError(message)
        definition = self._validated_definition(name, schema)

        with self._lock:
            existing = self._tools.get(name)
            if existing is not None and existing.toolset != toolset:
                message = (
                    f"Tool {name!r} (toolset {toolset!r}) would shadow the tool "
                    f"from toolset {existing.toolset!r}"
                )
                raise ToolRegistrationError(message)
            self._tools[name] = ToolEntry(
                name=name,
                toolset=toolset,
                definition=definition,
                handler=handler,
                check_fn=check_fn,
                is_async=is_async,
                description=description or definition["function"].get("description", ""),
            )

    def _validated_definition(self, name: str, schema: FunctionDefinition) -> ToolDefinition:
        # Widen at the runtime boundary: callers are not obliged to use mypy.
        raw_schema = cast(object, schema)
        if not isinstance(raw_schema, dict):
            message = f"Tool {name!r}: schema must be a dict"
            raise ToolRegistrationError(message)
        if raw_schema.get("name", name) != name:
            message = f"Tool {name!r}: schema name {raw_schema.get('name')!r} does not match"
            raise ToolRegistrationError(message)
        definition = cast(
            ToolDefinition, {"type": "function", "function": {**raw_schema, "name": name}}
        )
        try:
            # Reuse the transport's validation so a declaration that the request
            # builder would reject fails here, naming the tool, not mid-turn.
            self._transport.convert_tools([definition])
        except ModelInputError as exc:
            message = f"Tool {name!r}: invalid schema: {exc}"
            raise ToolRegistrationError(message) from exc
        return copy.deepcopy(definition)

    def deregister(self, name: str) -> None:
        """Remove a tool; removing an unknown name is a no-op."""
        with self._lock:
            self._tools.pop(name, None)

    def _snapshot(self) -> dict[str, ToolEntry]:
        with self._lock:
            return dict(self._tools)

    def get_entry(self, name: str) -> ToolEntry | None:
        """Return the entry registered under ``name``, if any."""
        return self._snapshot().get(name)

    def get_all_tool_names(self) -> list[str]:
        """Return every registered tool name, sorted."""
        return sorted(self._snapshot())

    def get_registered_toolset_names(self) -> list[str]:
        """Return every toolset that has at least one tool, sorted."""
        return sorted({entry.toolset for entry in self._snapshot().values()})

    def get_tool_names_for_toolset(self, toolset: str) -> list[str]:
        """Return the names of the tools in ``toolset``, sorted."""
        return sorted(entry.name for entry in self._snapshot().values() if entry.toolset == toolset)

    def get_definitions(self, tool_names: Iterable[str]) -> list[ToolDefinition]:
        """Return declarations for the requested tools whose check passes.

        Unknown names are ignored. Each probe runs at most once per call, and
        the result is sorted by name and deeply copied so callers may mutate it.
        """
        entries = self._snapshot()
        check_results: dict[AvailabilityCheck, bool] = {}
        definitions: list[ToolDefinition] = []
        for name in sorted(set(tool_names)):
            entry = entries.get(name)
            if entry is None:
                continue
            if entry.check_fn is not None:
                if entry.check_fn not in check_results:
                    check_results[entry.check_fn] = _run_check_fn(entry.check_fn)
                if not check_results[entry.check_fn]:
                    logger.debug("Tool %s unavailable (check failed)", name)
                    continue
            definitions.append(copy.deepcopy(entry.definition))
        return definitions

    async def dispatch(self, name: str, args: JsonObject) -> str:
        """Run a tool and return its result, never raising for tool failures.

        Unknown tools, handler exceptions and results that are not strings all
        become bounded ``{"error": ...}`` JSON the model can read. Cancellation
        still propagates.
        """
        entry = self.get_entry(name)
        if entry is None:
            return tool_error(f"Unknown tool: {name}")
        try:
            if entry.is_async:
                result: object = await cast(AsyncToolHandler, entry.handler)(args)
            else:
                handler = cast(SyncToolHandler, entry.handler)
                result = await asyncio.to_thread(handler, args)
        except Exception as exc:
            # Tool handlers are the outermost boundary of arbitrary tool code: a
            # failure is reported to the model as a result, never to the loop.
            logger.exception("Tool %s dispatch error: %s", name, _bound_error_text(str(exc)))
            return tool_error(
                _sanitize_tool_error(f"Tool execution failed: {type(exc).__name__}: {exc}")
            )
        return self._normalize_handler_result(name, result)

    @staticmethod
    def _normalize_handler_result(name: str, result: object) -> str:
        """Turn anything but a string into an error the model can read."""
        if isinstance(result, str):
            return _bound_json_error_result(result)
        if inspect.iscoroutine(result):
            # A sync-declared handler returned a coroutine; close it so it is
            # neither run nor reported as "never awaited".
            result.close()
        result_type = type(result).__name__
        logger.error("Tool %s handler returned unsupported result type: %s", name, result_type)
        return tool_error(
            f"Tool handler returned unsupported result type: {result_type}",
            error_type="tool_result_contract",
            tool=name,
            result_type=result_type,
        )


registry = ToolRegistry()
"""Process-wide default registry that built-in tool modules register into."""
