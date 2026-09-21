# Ported from hermes-agent agent/tool_executor.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Tool-call execution: sequential and concurrent dispatch, extracted from AIAgent.

Functions take the parent ``AIAgent`` first. Every call's identity travels as a
``_ToolCallRef``; both executors end in the same commit step so the tool-result wire
shape is produced once.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mertina.agent.display import (
    _detect_tool_failure,
)
from mertina.agent.message_sanitization import coalesce_tool_call_id
from mertina.agent.tool_dispatch_helpers import (
    make_tool_result_message,
)

logger = logging.getLogger(__name__)


_pairing_tool_call_id = (
    coalesce_tool_call_id  # canonical id used by the persisted assistant message
)


def _tc_name(tool_call: Any) -> str:
    return getattr(getattr(tool_call, "function", None), "name", "") or "tool"


_MAX_TOOL_WORKERS = 8  # concurrent worker threads per batch


def _parse_tool_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None]:
    """Parse model-emitted arguments without repairing or coercing them."""
    try:
        arguments = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError):
        arguments = None
    if isinstance(arguments, dict):
        return arguments, None
    return {}, json.dumps(
        {
            "error": "Invalid tool arguments",
            "message": "Tool arguments must be a valid JSON object; tool was not executed.",
        },
        ensure_ascii=False,
    )


def _max_workers_for_tool_batch(runnable_calls: list[Any]) -> int:
    """Return the worker cap for a concurrent tool batch."""
    if not runnable_calls:
        return 0
    max_workers = _MAX_TOOL_WORKERS
    return min(len(runnable_calls), max_workers)


@dataclass
class _ToolCallRef:
    """Identity of one tool call as every result message sees it: the name and args, the
    task, the pairing id and the request trace."""

    name: str
    args: dict[str, Any]
    task_id: str
    call_id: str
    trace: list[dict[str, Any]] | None

    def middleware_kwargs(self) -> dict[str, Any]:
        """Keyword form ``_run_agent_tool_execution_middleware`` (and tests patching it) expect."""
        return {
            "function_name": self.name,
            "function_args": self.args,
            "effective_task_id": self.task_id,
            "tool_call_id": self.call_id,
            "middleware_trace": self.trace,
        }

    def emit_cancelled(self, agent: Any, start_time: float) -> str:
        """Synthesize the ``cancelled`` result for a KeyboardInterrupt mid-tool."""
        message = "Tool execution cancelled by user interrupt"
        result = json.dumps({"error": message, "status": "cancelled"}, ensure_ascii=False)
        return result


def _append_skipped_tool_results(
    agent: Any,
    messages: list[dict[str, Any]],
    tool_calls: list[Any],
    effective_task_id: str,
    *,
    content: str,
) -> bool:
    """Append one ``tool`` result per unstarted call so the assistant tool-call turn never
    lacks matching results (role alternation). ``content`` is formatted with ``{name}``."""
    for tc in tool_calls:
        name = _tc_name(tc)
        result = content.format(name=name)
        messages.append(
            make_tool_result_message(
                name, result, _pairing_tool_call_id(tc), effect_disposition="none"
            )
        )
    return True


@dataclass
class _ParsedCall:
    """One model tool call after arg parsing."""

    tool_call: Any
    name: str
    args: dict[str, Any]
    middleware_trace: list[dict[str, Any]]
    parse_error: str | None

    def ref(self, task_id: str) -> _ToolCallRef:
        return _ToolCallRef(
            self.name,
            self.args,
            task_id,
            _pairing_tool_call_id(self.tool_call),
            self.middleware_trace,
        )


def _parse_tool_call(agent: Any, tool_call: Any) -> _ParsedCall:
    name = tool_call.function.name
    args, parse_error = _parse_tool_arguments(tool_call.function.arguments)
    return _ParsedCall(tool_call, name, args, [], parse_error)


@dataclass
class _ManagedToolResult:
    result: Any
    args: dict[str, Any]
    middleware_trace: list[dict[str, Any]] | None
    blocked: bool
    dispatched: bool


def _dispatch_authorized_once(
    agent: Any,
    state: _ManagedToolResult,
    ref: _ToolCallRef,
    *,
    execute: Callable[[dict[str, Any]], Any],
) -> Any:
    """The one real dispatch."""
    return execute(ref.args)


def _run_agent_tool_execution_middleware(
    agent: Any,
    *,
    function_name: str,
    function_args: dict[str, Any],
    effective_task_id: str,
    tool_call_id: str,
    execute: Callable[[dict[str, Any]], Any],
    middleware_trace: list[dict[str, Any]] | None = None,
) -> _ManagedToolResult:
    """Dispatch exactly once."""
    trace = middleware_trace if middleware_trace is not None else []
    state = _ManagedToolResult(
        result=None, args=function_args, middleware_trace=trace, blocked=False, dispatched=False
    )
    dispatch_lock = threading.Lock()

    def _authorized_dispatch(final_args: dict[str, Any]) -> Any:
        with dispatch_lock:
            if state.dispatched:
                raise RuntimeError("Hermes tool execution callback invoked more than once")
            state.dispatched = True
            state.blocked = False
            state.args = final_args
        return _dispatch_authorized_once(
            agent,
            state,
            _ToolCallRef(function_name, final_args, effective_task_id, tool_call_id, trace),
            execute=execute,
        )

    state.result = _authorized_dispatch(function_args)
    return state


def _run_sequential_tool_execution_middleware(
    agent: Any,
    *,
    function_name: str,
    function_args: dict[str, Any],
    effective_task_id: str,
    tool_call_id: str,
    execute: Callable[[dict[str, Any]], Any],
    middleware_trace: list[dict[str, Any]] | None = None,
) -> _ManagedToolResult:
    """Run one sequential call inline on the calling thread."""
    ref = _ToolCallRef(
        function_name, function_args, effective_task_id, tool_call_id, middleware_trace
    )
    kwargs = dict(
        ref.middleware_kwargs(),
        execute=execute,
    )
    return _run_agent_tool_execution_middleware(agent, **kwargs)


def _commit_tool_result(
    agent: Any,
    messages: list[dict[str, Any]],
    ref: _ToolCallRef,
    function_result: Any,
    *,
    tool_duration: float,
    is_error: bool,
    blocked: bool,
    effect_disposition: str | None,
    observed: bool = False,
    error_preview: Callable[[Any], Any] = lambda result: result,
    success_log_chars: int | None = None,
    verbose_text: Callable[[Any], Any] = lambda result: result,
) -> tuple[Any, Any, Any]:
    """Log the outcome (``observed`` results only), then wrap and append the result.

    ``success_log_chars`` (sequential path) also logs the completion line. Returns
    ``(persisted_result, display_result, risk_metadata)``.
    """
    function_name, tool_call_id = (
        ref.name,
        ref.call_id,
    )
    if observed:
        if is_error:
            logger.warning(
                "Tool %s returned error (%.2fs): %s",
                function_name,
                tool_duration,
                error_preview(function_result),
            )
        elif success_log_chars is not None:
            logger.info(
                "tool %s completed (%.2fs, %d chars)",
                function_name,
                tool_duration,
                success_log_chars,
            )
        if agent.verbose_logging:
            logging.debug("Tool %s completed in %.2fs", function_name, tool_duration)
            _log_result = verbose_text(function_result)
            logging.debug("Tool result (%d chars): %s", len(_log_result), _log_result)

    persisted_result = function_result

    # Multimodal dicts become an OpenAI-style content list; text-only servers get a
    # string-safe fallback so a rejected image result never poisons history.
    _tool_content = agent._tool_result_content_for_active_model(function_name, persisted_result)
    tool_message = make_tool_result_message(
        function_name, _tool_content, tool_call_id, effect_disposition=effect_disposition
    )
    messages.append(tool_message)
    return persisted_result, function_result, tool_message.get("_tool_output_risk")


# ── Concurrent batch machinery ──────────────────────────────────────────────


@dataclass
class _ToolOutcome:
    """One finished worker slot of a concurrent batch (``ref`` holds the final name/args/trace)."""

    ref: _ToolCallRef
    result: Any
    duration: float
    is_error: bool
    blocked: bool


class _ConcurrentBatch:
    """Shared state of one concurrent tool batch: per-slot results."""

    def __init__(
        self,
        agent: Any,
        messages: list[dict[str, Any]],
        effective_task_id: str,
        parsed_calls: list[_ParsedCall],
    ) -> None:
        self.agent = agent
        self.messages = messages
        self.effective_task_id = effective_task_id
        self.parsed_calls = parsed_calls
        self.results: list[_ToolOutcome | None] = [None] * len(parsed_calls)
        for i, pc in enumerate(parsed_calls):
            if pc.parse_error is not None:
                self.results[i] = _ToolOutcome(
                    pc.ref(effective_task_id), pc.parse_error, 0.0, True, True
                )

    def _dispatch_worker(self, index: int, ref: _ToolCallRef) -> _ToolOutcome | None:
        """Run one call and synthesize its slot outcome."""
        agent = self.agent
        start = time.time()
        blocked = False
        try:
            managed = _run_agent_tool_execution_middleware(
                agent,
                **ref.middleware_kwargs(),
                execute=lambda next_args: agent._invoke_tool(
                    ref.name,
                    next_args,
                    ref.task_id,
                    ref.call_id,
                    messages=self.messages,
                ),
            )
            result, ref.args, ref.trace = managed.result, managed.args, managed.middleware_trace
            blocked = managed.blocked
        except KeyboardInterrupt:
            with contextlib.suppress(Exception):
                agent.interrupt("keyboard interrupt")
            result = ref.emit_cancelled(agent, start)
            duration = time.time() - start
            logger.info("tool %s cancelled (%.2fs)", ref.name, duration)
            return _ToolOutcome(ref, result, duration, True, False)
        except Exception as tool_error:
            result = f"Error executing tool '{ref.name}': {tool_error}"
            logger.error("_invoke_tool raised for %s: %s", ref.name, tool_error, exc_info=True)
        duration = time.time() - start
        is_error, _ = _detect_tool_failure(ref.name, result)
        if is_error:
            logger.info("tool %s failed (%.2fs): %s", ref.name, duration, str(result)[:200])
        else:
            result_chars = len(result) if isinstance(result, str) else len(str(result))
            logger.info("tool %s completed (%.2fs, %d chars)", ref.name, duration, result_chars)
        return _ToolOutcome(ref, result, duration, is_error, blocked)

    def run_worker(self, index: int) -> None:
        """Worker function executed in a thread."""
        pc = self.parsed_calls[index]
        outcome = self._dispatch_worker(index, pc.ref(self.effective_task_id))
        if outcome is not None:
            self.results[index] = outcome

    def submit_all(
        self, executor: concurrent.futures.Executor, runnable: list[int]
    ) -> tuple[list[concurrent.futures.Future[None]], dict[concurrent.futures.Future[None], int]]:
        """Submit every runnable slot."""
        futures: list[concurrent.futures.Future[None]] = []
        future_to_index: dict[concurrent.futures.Future[None], int] = {}
        for i in runnable:
            f = executor.submit(self.run_worker, i)
            futures.append(f)
            future_to_index[f] = i
        return futures, future_to_index

    def await_completion(
        self,
        futures: list[concurrent.futures.Future[None]],
        future_to_index: dict[concurrent.futures.Future[None], int],
    ) -> bool:
        """Wait with periodic interrupt checks; True when the batch was abandoned
        (interrupt) and the executor must not join its workers."""
        agent = self.agent
        while True:
            wait_timeout = 5.0
            _done, not_done = concurrent.futures.wait(futures, timeout=wait_timeout)
            if not not_done:
                return False

            if agent._interrupt_requested:
                # Tools without interrupt checks (web_search, read_file) run to
                # completion; cancel unstarted futures so we don't block on them.
                agent._vprint(
                    f"{agent.log_prefix}⚡ Interrupt: cancelling {len(not_done)} pending concurrent tool(s)",  # noqa: E501  # upstream's message
                    force=True,
                )
            else:
                continue
            for f in not_done:
                f.cancel()
            # Give running tools a moment to notice the per-thread interrupt and exit gracefully.
            concurrent.futures.wait(not_done, timeout=3.0)
            return True

    def run(self) -> None:
        """Dispatch the runnable calls on a daemon pool and wait for the batch."""
        runnable = [i for i, pc in enumerate(self.parsed_calls) if pc.parse_error is None]
        if not runnable:
            return
        max_workers = _max_workers_for_tool_batch(
            [(i, None, self.parsed_calls[i].name) for i in runnable]
        )
        # Daemon workers: the stdlib pool's atexit join would let one wedged tool block exit.
        from mertina.tools.daemon_pool import DaemonThreadPoolExecutor

        executor = DaemonThreadPoolExecutor(max_workers=max_workers)
        abandon_executor = False
        try:
            futures, future_to_index = self.submit_all(executor, runnable)
            abandon_executor = self.await_completion(futures, future_to_index)
        finally:
            # An abandoning exit leaves wedged threads detached rather than joining them;
            # normal completion joins.
            executor.shutdown(wait=not abandon_executor, cancel_futures=abandon_executor)


def _unfinished_tool_result(agent: Any, ref: _ToolCallRef) -> tuple[str, float, str | None]:
    """Synthesize the result for a slot no worker filled (interrupt, or a thread that never
    returned) and return ``(function_result, tool_duration, effect_disposition)``."""
    if agent._interrupt_requested:
        function_result = (
            f"[Tool execution cancelled — {ref.name} was skipped due to user interrupt]"
        )
        tool_duration, effect_disposition = 0.0, None
    else:
        function_result = f"Error executing tool '{ref.name}': thread did not return a result"
        tool_duration, effect_disposition = 0.0, None
    return function_result, tool_duration, effect_disposition


def _append_batch_results(
    agent: Any, messages: list[dict[str, Any]], effective_task_id: str, batch: _ConcurrentBatch
) -> bool:
    """Append every slot's result in original call order."""
    for i, pc in enumerate(batch.parsed_calls):
        r = batch.results[i]
        # A worker may finish between the interrupt and this loop;
        # prefer its real result over a fabricated cancellation.
        if r is None:
            ref, is_error, blocked = pc.ref(effective_task_id), True, False
            function_result, tool_duration, effect_disposition = _unfinished_tool_result(
                agent,
                ref,
            )
        else:
            ref, function_result, tool_duration, is_error, blocked = (
                r.ref,
                r.result,
                r.duration,
                r.is_error,
                r.blocked,
            )
            effect_disposition = "none" if blocked else None
        _commit_tool_result(
            agent,
            messages,
            ref,
            function_result,
            tool_duration=tool_duration,
            is_error=is_error,
            blocked=blocked,
            effect_disposition=effect_disposition,
            observed=r is not None,
        )
    return True


def execute_tool_calls_concurrent(
    agent: Any,
    assistant_message: Any,
    messages: list[dict[str, Any]],
    effective_task_id: str,
    api_call_count: int = 0,
) -> None:
    """Execute tool calls concurrently; results are appended in original call order."""
    tool_calls = assistant_message.tool_calls
    num_tools = len(tool_calls)

    if agent._interrupt_requested:
        print(f"{agent.log_prefix}⚡ Interrupt: skipping {num_tools} tool call(s)")
        _append_skipped_tool_results(
            agent,
            messages,
            tool_calls,
            effective_task_id,
            content="[Tool execution cancelled — {name} was skipped due to user interrupt]",
        )
        return

    parsed_calls = [_parse_tool_call(agent, tc) for tc in tool_calls]

    batch = _ConcurrentBatch(agent, messages, effective_task_id, parsed_calls)

    batch.run()

    if not _append_batch_results(agent, messages, effective_task_id, batch):
        return


# ── Sequential dispatch ─────────────────────────────────────────────────────


@dataclass
class _SequentialDispatch:
    """How one sequential call executes: the callable plus its error policy."""

    execute: Callable[[dict[str, Any]], Any]
    middleware_trace_arg: list[dict[str, Any]] | None = (
        None  # forwarded to the middleware runner (registry closure reads it)
    )
    error_result: Callable[[Exception], str] | None = (
        None  # None → exceptions propagate (inline/delegate own failures)
    )
    error_log: str = ""
    handles_keyboard_interrupt: bool = False


def _resolve_sequential_dispatch(
    agent: Any, ref: _ToolCallRef, messages: list[dict[str, Any]]
) -> _SequentialDispatch:
    """Pick the execute callable for one sequential call: the registry."""
    function_name, effective_task_id, tool_call_id, middleware_trace = (
        ref.name,
        ref.task_id,
        ref.call_id,
        ref.trace,
    )

    def _execute(next_args: dict[str, Any]) -> Any:
        from mertina import model_tools

        return model_tools.handle_function_call(
            function_name,
            next_args,
            effective_task_id,
            tool_call_id=tool_call_id,
            session_id=agent.session_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
        )

    return _SequentialDispatch(
        execute=_execute,
        middleware_trace_arg=middleware_trace,
        error_result=lambda e: f"Error executing tool '{function_name}': {e}",
        error_log="handle_function_call raised for %s: %s",
        handles_keyboard_interrupt=True,
    )


def _skip_remaining_sequential(
    agent: Any,
    messages: list[dict[str, Any]],
    remaining: list[Any],
    effective_task_id: str,
    *,
    notice: str,
    **skip_kwargs: Any,
) -> bool:
    """Announce an interrupt and append one skipped result per unstarted call."""
    agent._vprint(f"{agent.log_prefix}⚡ Interrupt: skipping {len(remaining)} {notice}", force=True)
    return _append_skipped_tool_results(
        agent, messages, remaining, effective_task_id, **skip_kwargs
    )


def _append_invalid_arguments_result(
    agent: Any, messages: list[dict[str, Any]], ref: _ToolCallRef, parse_error: str
) -> None:
    """Append the parse-error result for a call whose arguments were not a JSON object."""
    messages.append(make_tool_result_message(ref.name, parse_error, ref.call_id))


def _run_sequential_call(
    agent: Any,
    dispatch: _SequentialDispatch,
    ref: _ToolCallRef,
    *,
    messages: list[dict[str, Any]],
    remaining_calls: list[Any],
    tool_start_time: float,
) -> tuple[_ManagedToolResult, float]:
    """Run one sequential call with its error policy; returns ``(managed, duration)``.
    KeyboardInterrupt (registry tools only) emits results for THIS and every remaining call
    before re-raising so the tool-call turn keeps matching results (alternation)."""
    try:
        managed = _run_sequential_tool_execution_middleware(
            agent,
            **dict(ref.middleware_kwargs(), middleware_trace=dispatch.middleware_trace_arg),
            execute=dispatch.execute,
        )
        ref.args = managed.args
    except KeyboardInterrupt:
        if not dispatch.handles_keyboard_interrupt:
            raise
        with contextlib.suppress(Exception):
            agent.interrupt("keyboard interrupt")
        _append_skipped_tool_results(
            agent,
            messages,
            remaining_calls,
            ref.task_id,
            content="[Tool execution cancelled — {name} was skipped due to keyboard interrupt]",
        )
        raise
    except Exception as tool_error:
        if dispatch.error_result is None:
            raise
        function_result = dispatch.error_result(tool_error)
        logger.error(dispatch.error_log, ref.name, tool_error, exc_info=True)
        managed = _ManagedToolResult(
            result=function_result,
            args=ref.args,
            middleware_trace=ref.trace,
            blocked=False,
            dispatched=False,
        )
    finally:
        tool_duration = time.time() - tool_start_time
    return managed, tool_duration


def _publish_sequential_result(
    agent: Any,
    messages: list[dict[str, Any]],
    ref: _ToolCallRef,
    managed: _ManagedToolResult,
    *,
    tool_duration: float,
    index: int,
) -> bool:
    """Observe → commit for one sequential result."""
    ref.args, ref.trace, function_result = managed.args, managed.middleware_trace, managed.result
    # Multimodal dict results (_multimodal=True) are not sliceable as strings.
    _result_len = (
        len(function_result) if isinstance(function_result, str) else len(str(function_result))
    )
    _is_error_result, _ = _detect_tool_failure(ref.name, function_result)
    _commit_tool_result(
        agent,
        messages,
        ref,
        function_result,
        tool_duration=tool_duration,
        is_error=_is_error_result,
        blocked=managed.blocked,
        effect_disposition=None,
        observed=True,
        error_preview=lambda res: (
            res[:200] if isinstance(res, str) and not agent.verbose_logging else res
        ),
        success_log_chars=_result_len,
    )
    return True


def execute_tool_calls_sequential(
    agent: Any,
    assistant_message: Any,
    messages: list[dict[str, Any]],
    effective_task_id: str,
    api_call_count: int = 0,
) -> None:
    _execute_tool_calls_sequential(
        agent,
        assistant_message,
        messages,
        effective_task_id,
        api_call_count,
    )


def _execute_tool_calls_sequential(
    agent: Any,
    assistant_message: Any,
    messages: list[dict[str, Any]],
    effective_task_id: str,
    api_call_count: int = 0,
) -> None:
    """Execute tool calls sequentially (single calls or interactive tools)."""
    tool_calls = assistant_message.tool_calls

    for i, tool_call in enumerate(tool_calls, 1):
        # Check interrupt BEFORE each tool so a "stop" during the previous one skips the rest.
        if agent._interrupt_requested:
            if not _skip_remaining_sequential(
                agent,
                messages,
                tool_calls[i - 1 :],
                effective_task_id,
                notice="tool call(s)",
                content="[Tool execution cancelled — {name} was skipped due to user interrupt]",
            ):
                return
            break

        pc = _parse_tool_call(agent, tool_call)
        ref = pc.ref(effective_task_id)
        if pc.parse_error is not None:
            _append_invalid_arguments_result(agent, messages, ref, pc.parse_error)
            continue

        tool_start_time = time.time()
        dispatch = _resolve_sequential_dispatch(agent, ref, messages)
        managed, tool_duration = _run_sequential_call(
            agent,
            dispatch,
            ref,
            messages=messages,
            remaining_calls=tool_calls[i - 1 :],
            tool_start_time=tool_start_time,
        )
        if not _publish_sequential_result(
            agent, messages, ref, managed, tool_duration=tool_duration, index=i
        ):
            return

        if agent._interrupt_requested and i < len(tool_calls):
            if not _skip_remaining_sequential(
                agent,
                messages,
                tool_calls[i:],
                effective_task_id,
                notice="remaining tool call(s)",
                content="[Tool execution skipped — {name} was not started. User sent a new message]",  # noqa: E501  # upstream's message
            ):
                return
            break


__all__ = [
    "execute_tool_calls_concurrent",
    "execute_tool_calls_sequential",
]
