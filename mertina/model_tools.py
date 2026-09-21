# Ported from hermes-agent model_tools.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Thin orchestration layer over the tool registry.

Importing runs tool discovery (each tools/*.py self-registers via
tools.registry.register()); exposes get_tool_definitions() (schemas sent to
the model) and handle_function_call() (dispatch) plus registry pass-throughs.
"""

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from mertina.tools.registry import _MAX_TOOL_ERROR_CHARS as _TOOL_ERROR_MAX_LEN
from mertina.tools.registry import (
    discover_builtin_tools,
    registry,
    tool_error,
)

logger = logging.getLogger(__name__)


# --- Async bridging (single source of truth; registry.dispatch uses it too) ---
# Loops are persistent (never asyncio.run per call): cached httpx/AsyncOpenAI
# clients stay bound to a live loop, so their GC cleanup can't hit "Event loop
# is closed". Main thread shares one loop; worker threads own thread-local loops.

_tool_loop = None  # persistent loop for the main (CLI) thread
_tool_loop_lock = threading.Lock()
_worker_thread_local = threading.local()  # per-worker-thread persistent loops


def _get_tool_loop():
    """Long-lived event loop for async tool handlers on the main thread."""
    global _tool_loop
    with _tool_loop_lock:
        if _tool_loop is None or _tool_loop.is_closed():
            _tool_loop = asyncio.new_event_loop()
        return _tool_loop


def _get_worker_loop():
    """Persistent event loop for the current worker thread (thread-local)."""
    loop = getattr(_worker_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _worker_thread_local.loop = loop
    return loop


def _run_async(coro):
    """Run a coroutine from sync code; safe under a running loop (gateway/RL env)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Inside a running loop: run in a fresh thread whose loop we keep a
        # reference to, so on timeout we can cancel the task inside it
        # (ThreadPoolExecutor.cancel() is a no-op on a running worker).
        import concurrent.futures

        worker_loop: asyncio.AbstractEventLoop | None = None
        loop_ready = threading.Event()

        def _run_in_worker():
            nonlocal worker_loop
            worker_loop = asyncio.new_event_loop()
            loop_ready.set()
            try:
                asyncio.set_event_loop(worker_loop)
                return worker_loop.run_until_complete(coro)
            finally:
                try:  # drain tasks still pending after an external cancel
                    pending = asyncio.all_tasks(worker_loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        worker_loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:
                    pass
                worker_loop.close()

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_run_in_worker)
        try:
            return future.result(timeout=300)
        except concurrent.futures.TimeoutError:
            # Cancel inside the worker's own loop so the thread can wind down.
            if loop_ready.wait(timeout=1.0) and worker_loop is not None:
                try:
                    for t in asyncio.all_tasks(worker_loop):
                        worker_loop.call_soon_threadsafe(t.cancel)
                except RuntimeError:
                    pass  # loop already closed
            raise
        finally:
            pool.shutdown(wait=False)  # never block the caller on a stuck coroutine

    if threading.current_thread() is not threading.main_thread():
        return _get_worker_loop().run_until_complete(coro)
    return _get_tool_loop().run_until_complete(coro)


# --- Tool discovery (importing each tools/*.py triggers registry.register) ---
discover_builtin_tools()


# Tool names from the last get_tool_definitions() call.
_last_resolved_tool_names: list[str] = []


# --- get_tool_definitions (the main schema provider) --------------------------


def get_tool_definitions(
    quiet_mode: bool = False,
) -> list[dict[str, Any]]:
    """Tool definitions for model API calls: every registered tool whose check passes.

    quiet_mode suppresses status prints.
    """

    def compute():
        return _compute_tool_definitions(
            quiet_mode,
        )

    return compute()


def _compute_tool_definitions(
    quiet_mode: bool = False,
) -> list[dict[str, Any]]:
    """Implementation of :func:`get_tool_definitions`."""
    tools_to_include = set(registry.get_all_tool_names())
    filtered_tools = registry.get_definitions(tools_to_include, quiet=quiet_mode)
    global _last_resolved_tool_names
    _last_resolved_tool_names = [t["function"]["name"] for t in filtered_tools]

    if not quiet_mode:
        print(
            f"🛠️  Final tool selection ({len(filtered_tools)} tools): {', '.join(_last_resolved_tool_names)}"
            if filtered_tools
            else "🛠️  No tools selected (all filtered out or unavailable)"
        )
    return filtered_tools


# =============================================================================
# handle_function_call  (the main dispatcher)
# =============================================================================


# --- Tool error sanitization --------------------------------------------------
# Defense-in-depth: strip role tags / CDATA / code fences from exception text the
# model will read, and cap length (cap shared with tools/registry.py so text never
# passes two different caps with two different markers).
_TOOL_ERROR_STRIP_RES = (
    re.compile(
        r"</?(?:tool_call|function_call|result|response|output|input|system|assistant|user)>",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*```(?:json|xml|html|markdown)?\s*", re.MULTILINE),
    re.compile(r"\s*```\s*$", re.MULTILINE),
    re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL),
)


def _sanitize_tool_error(error_msg: str) -> str:
    """Strip structural framing tokens from a tool error before the model sees it."""
    if not error_msg:
        return "[TOOL_ERROR] "
    sanitized = error_msg
    for pattern in _TOOL_ERROR_STRIP_RES:
        sanitized = pattern.sub("", sanitized)
    if len(sanitized) > _TOOL_ERROR_MAX_LEN:
        sanitized = sanitized[: _TOOL_ERROR_MAX_LEN - 3] + "..."
    return f"[TOOL_ERROR] {sanitized}"


@dataclass(frozen=True)
class _CallIds:
    """Identity fields of one tool call."""

    task_id: str | None = None
    session_id: str | None = None
    tool_call_id: str | None = None
    turn_id: str | None = None
    api_request_id: str | None = None


def _execute_tool(
    function_name: str,
    function_args: dict[str, Any],
    ids: _CallIds,
    *,
    user_task: str | None,
) -> Any:
    """Run the registry handler."""
    dispatch_kwargs: dict[str, Any] = {"task_id": ids.task_id, "session_id": ids.session_id}
    dispatch_kwargs["user_task"] = user_task

    def _dispatch(next_args: dict[str, Any]) -> Any:
        return registry.dispatch(function_name, next_args, **dispatch_kwargs)

    return _dispatch(function_args)


def handle_function_call(
    function_name: str,
    function_args: dict[str, Any],
    task_id: str | None = None,
    tool_call_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    api_request_id: str | None = None,
    user_task: str | None = None,
) -> str:
    """Route a tool call to the registry; returns a JSON string.

    task_id and session_id are passed on to the handler; user_task feeds
    browser_snapshot.
    """
    if not isinstance(function_args, dict):
        function_args = {}
    ids = _CallIds(task_id, session_id, tool_call_id, turn_id, api_request_id)

    try:
        result = _execute_tool(
            function_name,
            function_args,
            ids,
            user_task=user_task,
        )
        return result

    except Exception as e:
        error_msg = f"Error executing {function_name}: {e!s}"
        logger.exception(error_msg)
        return tool_error(_sanitize_tool_error(error_msg))


# =============================================================================
# Backward-compat wrapper functions (registry pass-throughs)
# =============================================================================


def get_toolset_for_tool(tool_name: str) -> str | None:
    return registry.get_toolset_for_tool(tool_name)


def check_toolset_requirements() -> dict[str, bool]:
    """{toolset: available_bool} for every registered toolset."""
    return registry.check_toolset_requirements()
