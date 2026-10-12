"""Tool-call execution: sequential and concurrent dispatch, extracted from AIAgent.

Functions take the parent ``AIAgent`` first; ``run_agent`` keeps thin wrappers and is
reached lazily via ``_ra()`` so ``run_agent._set_interrupt`` patches still work. Every
call's identity travels as a ``_ToolCallRef``; both executors end in the same
observe → commit → project pipeline so the tool-result wire shape is produced once.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agent.display import (
    KawaiiSpinner,
    build_tool_preview as _build_tool_preview,
    build_tool_label as _build_tool_label,
    get_cute_tool_message as _get_cute_tool_message_impl,
    get_tool_emoji as _get_tool_emoji,
    redact_tool_args_for_display as _redact_tool_args_for_display,
    _detect_tool_failure,
)
from agent.message_sanitization import coalesce_tool_call_id
from agent.inline_tool_executors import (
    emit_terminal_post_tool_call,
)
from agent.tool_dispatch_helpers import (
    _multimodal_text_summary,
    make_tool_result_message,
)
from tools.thread_context import propagate_context_to_thread
from tools.budget_config import BudgetConfig, DEFAULT_BUDGET, budget_for_context_window

_LARGE_TOOL_RESULT_TRIM_CHARS = 1_000_000

logger = logging.getLogger(__name__)


_pairing_tool_call_id = coalesce_tool_call_id  # canonical id used by the persisted assistant message








def _budget_for_agent(agent) -> BudgetConfig:
    """Tool-result BudgetConfig scaled to the agent's context window. Unknown length goes
    through ``budget_for_context_window(None)`` (not DEFAULT_BUDGET) so the MCP threshold
    override still applies.

    Large-context models keep the historical 100K/200K char defaults; small models (e.g. a 65K-token local
    model switched into mid-session) get a budget proportional to their window so a single large tool result
    can't push the request past the model's limit (#23767). Falls back to the default budget when the
    context length isn't resolvable.
    """
    try:
        ctx = getattr(getattr(agent, "context_compressor", None), "context_length", None)
        return budget_for_context_window(int(ctx) if ctx else None)
    except Exception:
        return DEFAULT_BUDGET

_MAX_TOOL_WORKERS = 8  # concurrent worker threads per batch
_DEFAULT_IMAGE_PARALLEL_REQUESTS = 4
_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S = 420.0
_START_ORDER_GATE_TIMEOUT_S = 120.0
_AUTHORIZATION_GATE_LOCK_TIMEOUT_S = 360.0


def _authorization_gate_lock_timeout() -> float:
    """Authorization-lock bound = ``tools.approval_human_wait.human_wait_ceiling`` (approval timeout +
    margin, capped so it can't overflow Lock.acquire): never break serialization while a
    prompt is answerable, never let a wedged holder park workers forever. Deliberately NOT
    min()'d with the fallback so the gate never gives up early.

    Delegates to ``tools.approval_human_wait.human_wait_ceiling`` — the same bound that clamps a human-wait window's
    deadline contribution — so the two can't drift. Long enough that serialization is never broken while a
    legitimate approval prompt is still answerable; short enough that a wedged holder (hanging
    ``pre_tool_call`` plugin, dead approval client) cannot park other workers forever (#79719). Resolved
    once per gate (per batch), so a mid-process ``approvals.timeout`` change applies from the next batch.
    """
    try:
        from tools.approval_human_wait import human_wait_ceiling

        # human_wait_ceiling is platform-safety-capped (agent/deadline.py MAX_SAFE_TIMEOUT_S): a huge
        # approvals.timeout can no longer overflow Lock.acquire's time_t on macOS (#83220). Deliberately NOT
        # min()'d with _AUTHORIZATION_GATE_LOCK_TIMEOUT_S — the gate must never give up while a legitimate
        # approval prompt is still answerable (#79719), so a configured approvals.timeout above 360s must
        # extend the gate.
        return human_wait_ceiling()
    except Exception:
        return _AUTHORIZATION_GATE_LOCK_TIMEOUT_S


class _BatchAbandoned(BaseException):
    """Raised inside a worker when the batch was abandoned before dispatch; a BaseException
    so ``except Exception`` handlers in the middleware chain can't swallow it."""


def _parse_tool_arguments(raw_arguments: Any) -> tuple[dict, Optional[str]]:
    """Parse model-emitted arguments without repairing or coercing them."""
    try:
        arguments = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError):
        arguments = None
    if isinstance(arguments, dict):
        return arguments, None
    return {}, json.dumps(
        {"error": "Invalid tool arguments", "message": "Tool arguments must be a valid JSON object; tool was not executed."},
        ensure_ascii=False,
    )


def _resolve_concurrent_tool_timeout() -> float | None:
    """Per-batch concurrent deadline: ``timeouts.tools.concurrent_batch`` wins,
    ``HERMES_CONCURRENT_TOOL_TIMEOUT_S`` is the legacy bridge, ``0``/negative disables."""
    return None






def _max_workers_for_tool_batch(runnable_calls) -> int:
    """Return the worker cap for a concurrent tool batch."""
    if not runnable_calls:
        return 0
    max_workers = _MAX_TOOL_WORKERS
    if any((call[2] if len(call) >= 3 else None) == "image_generate" for call in runnable_calls):
        max_workers = min(max_workers, _image_generate_parallel_limit())
    return min(len(runnable_calls), max_workers)


def _ra():
    """Lazy reference to ``run_agent`` so patches like ``run_agent._set_interrupt`` work."""
    import run_agent
    return run_agent




_emit_terminal_post_tool_call = emit_terminal_post_tool_call


@dataclass
class _ToolCallRef:
    """Identity of one tool call as every hook / result message sees it: the (possibly
    middleware-rewritten) name and args, the task, the pairing id and the request trace."""

    name: str
    args: dict
    task_id: str
    call_id: str
    trace: list

    def middleware_kwargs(self) -> dict[str, Any]:
        """Keyword form ``_run_agent_tool_execution_middleware`` (and tests patching it) expect."""
        return {
            "function_name": self.name, "function_args": self.args, "effective_task_id": self.task_id,
            "tool_call_id": self.call_id, "middleware_trace": self.trace,
        }

    def emit_post(self, agent, result, *, trace=None, **outcome) -> None:
        """Emit the one terminal ``post_tool_call`` for this call (``outcome`` = status /
        error_type / error_message / duration_ms). Resolved through the module attribute so
        tests patching ``_emit_terminal_post_tool_call`` still intercept."""
        _emit_terminal_post_tool_call(
            agent,
            function_name=self.name,
            function_args=self.args,
            result=result,
            effective_task_id=self.task_id,
            tool_call_id=self.call_id,
            middleware_trace=list(self.trace if trace is None else trace),
            **outcome,
        )

    def emit_cancelled(self, agent, start_time: float) -> str:
        """Synthesize the ``cancelled`` result for a KeyboardInterrupt mid-tool and emit its hook."""
        message = "Tool execution cancelled by user interrupt"
        result = json.dumps({"error": message, "status": "cancelled"}, ensure_ascii=False)
        self.emit_post(
            agent, result, duration_ms=int((time.time() - start_time) * 1000),
            status="cancelled", error_type="keyboard_interrupt", error_message=message,
        )
        return result

    def emit_invalid_arguments(self, agent, result: str) -> None:
        self.emit_post(
            agent, result, trace=[],
            status="error", error_type="invalid_tool_arguments", error_message="Tool arguments must be a valid JSON object",
        )






def _canonical_tool_name(function_name: str) -> str:
    """Map legacy tool-name aliases BEFORE agent-loop dispatch."""
    from model_tools import _LEGACY_TOOL_ALIASES as _lta

    return _lta.get(function_name, function_name)


def _unwrap_tool_search_call(
    agent, function_name: str, function_args: dict, *, flatten_probe: bool = False
) -> tuple[str, dict, Optional[str]]:
    """Peel the ``tool_call`` bridge so downstream hooks (checkpointing, guardrails, plugin
    hooks, activity feed) see the underlying tool; ``tool_call.function`` stays untouched for
    the transcript and tool_call_id pairing.

    The unwrap bypasses handle_function_call's scope check, so session toolset scope is
    enforced HERE. Returns ``(name, args, scope_block)``; ``scope_block`` is the block
    message when the underlying tool is out of scope or its args fail the deferred-schema
    probe (``flatten_probe`` collapses the probe's JSON payload to one plain string for
    callers that wrap the message in ``{"error": ...}``).
    """
    scope_block: Optional[str] = None
    try:
        from tools import tool_search as _ts
        if function_name != _ts.TOOL_CALL_NAME:
            return function_name, function_args, None
        underlying, underlying_args, err = _ts.resolve_underlying_call(function_args)
        if err or not underlying:
            return function_name, function_args, None
        if underlying == _ts.CONNECTOR_BATCH_SENTINEL:
            # Both executors retain the wrapper: scope/probe/hooks run per entry
            # in the batch dispatcher, not against a synthetic registry name.
            return function_name, function_args, None
        if underlying not in _tool_search_scoped_names(agent):
            return function_name, function_args, (
                f"'{underlying}' is not available in this session. Use tool_search to find tools you can call."
            )
        # Validate before unwrapping: the generic bridge hides the concrete
        # parameter schema from provider-native tool-call validation.
        scope_block = _ts.validate_deferred_call_args(underlying, underlying_args)
        if scope_block is None:
            return underlying, underlying_args, None
        if flatten_probe:
            probe = json.loads(scope_block)
            scope_block = (
                f"{probe.get('error', '')} Parameters schema: "
                f"{json.dumps(probe.get('parameters', {}), ensure_ascii=False)}. "
                f"{probe.get('hint', '')}"
            ).strip()
    except Exception:
        pass
    return function_name, function_args, scope_block


@dataclass
class _ParsedCall:
    """One model tool call after alias canonicalization, arg parsing and bridge unwrap."""

    tool_call: Any
    name: str
    args: dict
    middleware_trace: list
    parse_error: Optional[str]
    scope_block: Optional[str]

    def ref(self, task_id: str) -> _ToolCallRef:
        return _ToolCallRef(self.name, self.args, task_id, _pairing_tool_call_id(self.tool_call), self.middleware_trace)


def _parse_tool_call(agent, tool_call, *, flatten_probe: bool = False) -> _ParsedCall:
    name = _canonical_tool_name(tool_call.function.name)
    args, parse_error = _parse_tool_arguments(tool_call.function.arguments)
    scope_block = None
    if parse_error is None:
        name, args, scope_block = _unwrap_tool_search_call(agent, name, args, flatten_probe=flatten_probe)
    return _ParsedCall(tool_call, name, args, [], parse_error, scope_block)


@dataclass
class _ManagedToolResult:
    result: Any
    args: dict[str, Any]
    middleware_trace: list[dict[str, Any]]
    blocked: bool
    dispatched: bool


class _ToolTimeoutResult(str):
    """Marker for a synthesized sequential-tool timeout result."""


class _ToolCancelledResult(str):
    """Marker for a synthesized sequential-tool user-interrupt result; its terminal
    post_tool_call was already emitted, so a late-finishing abandoned worker must not report."""


class _ConcurrentToolAuthorizationGate:
    """Serialize policy prompts and exclude human approval waits from batch deadlines.

    The acquire is BOUNDED: on expiry the worker prompts unserialized rather than starving
    the batch behind a wedged plugin/approval client. Exclusion is measured at the SOURCE
    of the human wait (``tools.approval.human_wait_seconds``), NOT as gate residency —
    residency-based exclusion let a wedged plugin keep the deadline from ever firing.

    Serialization keeps concurrent approval prompts from interleaving on the user's screen. The acquire is
    BOUNDED: a worker wedged inside the gate (a hanging ``pre_tool_call`` plugin, or an approval round-trip
    to a client that went away) must not park every other worker forever. On expiry the worker runs its
    prompt unserialized — worst case is interleaved prompts, strictly better than permanent starvation (same
    tradeoff as the start-order gate, #79705).
    Gate residency is arbitrary code — using it as the exclusion signal let a wedged plugin grow the
    exclusion 1:1 with wall clock, keeping the batch deadline's ``remaining`` constant so it never fired and
    the turn hung forever (#79719). A wedged plugin now contributes nothing to the exclusion and the batch
    times out normally, while a genuine approval wait (which can legitimately exceed any fixed bound) is
    still excluded in full.
    """

    def __init__(self, *, lock_timeout: float | None = None, session_key: str | None = None) -> None:
        self._serialization_lock = threading.Lock()
        self._lock_timeout = _authorization_gate_lock_timeout() if lock_timeout is None else lock_timeout
        self._session_key = session_key
        if self._session_key is None:
            # Snapshot on the SUBMITTING thread: excluded_seconds() is polled from the
            # batch wait loop, whose context may differ from the workers'.
            try:
                from tools.approval_context import get_current_session_key

                self._session_key = get_current_session_key()
            except Exception:
                logger.debug(
                    "authorization gate could not snapshot the session key; "
                    "human-wait exclusion will re-resolve it at poll time",
                    exc_info=True,
                )
        self._baseline_wait_seconds = self._human_wait_seconds()

    def _human_wait_seconds(self) -> float:
        try:
            from tools.approval_human_wait import human_wait_seconds

            return human_wait_seconds(self._session_key)
        except Exception:
            return 0.0

    def run(self, callback):
        if not self._serialization_lock.acquire(timeout=self._lock_timeout):
            # Deterministic failure (bad command, non-MCP URL, 401/403): every retry hits the same wall.
            # Park immediately instead of burning the retry ladder and spamming N identical warnings
            # (#65673). Auth failures park here too rather than returning. Returning ends the run task, and
            # with it the only listener on ``_reconnect_event`` — so a 401 on the very first connect left
            # the server unrevivable for the life of the process, even after the user re-authenticated with
            # ``hermes mcp login``. Parking keeps the task alive so the 300s self-probe (and an explicit
            # /mcp refresh) can pick up fresh tokens.
            logger.warning(
                "authorization gate lock not acquired after %.1fs "
                "(holder wedged in a pre_tool_call plugin or approval "
                "round-trip?); running prompt unserialized",
                self._lock_timeout,
            )
            return callback()
        try:
            return callback()
        finally:
            self._serialization_lock.release()

    def excluded_seconds(self) -> float:
        """Return human-approval wait seconds accrued since the batch started."""
        return max(0.0, self._human_wait_seconds() - self._baseline_wait_seconds)


@contextlib.contextmanager
def _registered_tool_worker(agent):
    """Track this worker tid for interrupt fan-out (``AIAgent.interrupt()``); on ANY exit
    (incl. BaseException) discard it and clear its interrupt bit so a recycled tid starts clean."""
    tid = threading.current_thread().ident
    with agent._tool_worker_threads_lock:
        agent._tool_worker_threads.add(tid)
    try:
        yield tid
    finally:
        with agent._tool_worker_threads_lock:
            agent._tool_worker_threads.discard(tid)
        with contextlib.suppress(Exception):
            _ra()._set_interrupt(False, tid)


_NO_REASON = object()




def _set_worker_activity_callback(agent) -> None:
    """The activity callback is thread-local: bind it on THIS thread so tool-layer heartbeats fire."""
    with contextlib.suppress(Exception):
        from tools.environments.base import set_activity_callback

        set_activity_callback(agent._touch_activity)


_TOOL_ACTIVITY_HEARTBEAT_INTERVAL_S = 30.0












def _run_agent_tool_execution_middleware(
    agent,
    *,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: str,
    execute,
    scope_block: str | None = None,
    display_index: int | None = None,
    middleware_trace: list[dict[str, Any]] | None = None,
    begin_execution=None,
    authorization_gate: _ConcurrentToolAuthorizationGate | None = None,
) -> _ManagedToolResult:
    trace = middleware_trace if middleware_trace is not None else []
    state = _ManagedToolResult(result=None, args=function_args, middleware_trace=trace, blocked=False, dispatched=False)
    ref = _ToolCallRef(function_name, function_args, effective_task_id, tool_call_id, trace)
    if begin_execution is not None:
        begin_execution(lambda: _begin_tool_execution(agent, ref, display_index))
    else:
        _begin_tool_execution(agent, ref, display_index)
    state.result = execute(function_args)
    state.dispatched = True
    return state


_SEQUENTIAL_INTERRUPT_POLL_SECONDS = 1.0




_SEQUENTIAL_DEADLINE_EXEMPT_TOOLS = frozenset({"delegate_task", "manage_connections"})








def _safe_callback(callback, label: str, *args, **kwargs) -> None:
    """Invoke a UI/bridge callback if set; a failing callback is logged, never fatal."""
    if not callback:
        return
    try:
        callback(*args, **kwargs)
    except Exception as callback_error:
        logging.debug("%s callback error: %s", label, callback_error)


def _begin_tool_execution(agent, ref: _ToolCallRef, display_index: int | None) -> None:
    """Run user-visible and checkpoint preflight on final tool arguments."""
    function_name, function_args, effective_task_id, tool_call_id = ref.name, ref.args, ref.task_id, ref.call_id
    display_args = _redact_tool_args_for_display(function_name, function_args) or function_args
    if _tool_progress_enabled(agent):
        prefix = f"Tool {display_index}" if display_index is not None else "Tool"
        if agent.verbose_logging:
            print(f"  📞 {prefix}: {function_name}({list(display_args.keys())})")
            print(agent._wrap_verbose("Args: ", json.dumps(display_args, indent=2, ensure_ascii=False)))
        else:
            print(f"  📞 {prefix}: {function_name}({list(function_args.keys())}) - {_preview(json.dumps(display_args, ensure_ascii=False), agent.log_prefix_chars)}")

    agent._current_tool = function_name
    agent._touch_activity(f"executing tool: {function_name}")
    _set_worker_activity_callback(agent)

    if agent.tool_progress_callback:
        try:
            preview = _build_tool_preview(function_name, display_args)
        except Exception as callback_error:
            logging.debug("Tool progress callback error: %s", callback_error)
        else:
            _safe_callback(agent.tool_progress_callback, "Tool progress", "tool.started", function_name, preview, display_args)
    _safe_callback(agent.tool_start_callback, "Tool start", tool_call_id, function_name, display_args)

def _emit_tool_complete_and_risk(agent, ref: _ToolCallRef, result, risk_metadata, blocked: bool) -> None:
    """Fire ``tool_complete_callback`` (unless blocked) then the ``tool.output_risk`` projection."""
    if not blocked and agent.tool_complete_callback:
        try:
            display_args = _redact_tool_args_for_display(ref.name, ref.args) or ref.args
        except Exception as cb_err:
            logging.debug("Tool complete callback error: %s", cb_err)
        else:
            _safe_callback(agent.tool_complete_callback, "Tool complete", ref.call_id, ref.name, display_args, result)
    if risk_metadata is not None and risk_metadata.get("risk") != "low":
        _safe_callback(
            agent.tool_progress_callback, "Tool output risk",
            "tool.output_risk", ref.name, None, None, tool_call_id=ref.call_id, risk_metadata=risk_metadata,
        )


def _commit_tool_result(
    agent,
    messages: list,
    ref: _ToolCallRef,
    function_result,
    *,
    budget: BudgetConfig,
    tool_duration: float,
    is_error: bool,
    blocked: bool,
    effect_disposition,
    observed: bool = False,
    error_preview: Callable[[Any], Any] = lambda result: result,
    success_log_chars: Optional[int] = None,
    verbose_text: Callable[[Any], Any] = lambda result: result,
):
    """Observe (``observed`` results only) and log the outcome; mark the tool done; persist/
    spill, hint, wrap and append the result; flush the session DB; project ``tool.completed``.

    Blocked calls never ran, so they are neither guardrail-observed nor fed to the file-
    mutation verifier; ``success_log_chars`` (sequential path) also logs the completion line.
    Returns ``(persisted_result, display_result, risk_metadata)`` (``display_result`` =
    pre-persist content for UI previews) or ``None`` when the flush failed (stop the batch).
    """
    function_name, function_args, tool_call_id, effective_task_id = ref.name, ref.args, ref.call_id, ref.task_id
    if observed:
        if is_error:
            logger.warning("Tool %s returned error (%.2fs): %s", function_name, tool_duration, error_preview(function_result))
        elif success_log_chars is not None:
            logger.info("tool %s completed (%.2fs, %d chars)", function_name, tool_duration, success_log_chars)
        if agent.verbose_logging:
            logging.debug("Tool %s completed in %.2fs", function_name, tool_duration)
            _log_result = verbose_text(function_result)
            logging.debug("Tool result (%d chars): %s", len(_log_result), _log_result)

    agent._current_tool = None
    _status_suffix = " (error)" if is_error else ""
    agent._touch_activity(f"tool completed: {function_name} ({tool_duration:.1f}s){_status_suffix}")

    persisted_result = function_result
    tool_message = make_tool_result_message(function_name, persisted_result, tool_call_id, effect_disposition=effect_disposition)
    messages.append(tool_message)

    if not blocked:
        # ``tool.completed`` projects AFTER the canonical append + flush so resume can
        # reconstruct the result even if the UI bridge dies mid-projection.
        _safe_callback(
            agent.tool_progress_callback, "Tool progress",
            "tool.completed", function_name, None, None, duration=tool_duration, is_error=is_error, result=function_result,
        )
    return persisted_result, function_result, tool_message.get("_tool_output_risk")




def _finalize_tool_batch(agent, messages: list, effective_task_id: str, num_tools: int, budget: BudgetConfig) -> None:
    return


def _tool_progress_enabled(agent) -> bool:
    return not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off"








@dataclass
class _ToolOutcome:
    """One finished worker slot of a concurrent batch (``ref`` holds the final name/args/trace)."""

    ref: _ToolCallRef
    result: Any
    duration: float
    is_error: bool
    blocked: bool


def _start_order_gate_timeout(batch_timeout: float | None) -> float:
    """The gate bound must sit UNDER the batch deadline, else parked workers are falsely
    reported timed out without starting. A disabled deadline keeps the stock bound."""
    if batch_timeout is None:
        return _START_ORDER_GATE_TIMEOUT_S
    return min(_START_ORDER_GATE_TIMEOUT_S, batch_timeout / 2)


class _StartOrderGate:
    """Serialize worker dispatch by submit order (prompts appear in call order); ``abandon()``
    releases every parked worker so none dispatches a tool the turn already gave up on."""

    def __init__(self, timeout: float) -> None:
        self._condition = threading.Condition()
        self._next_order = 0
        self._timeout = timeout
        self.abandoned = threading.Event()

    def abandon(self) -> None:
        self.abandoned.set()
        with self._condition:
            self._condition.notify_all()

    def begin_in_order(self, order: int, callback=None, *, tool_name: str = "") -> bool:
        """Wait for ``order``, run ``callback``, advance. Returns False if abandoned."""
        with self._condition:
            # Bounded wait so one wedged dispatch can't starve later-ordered workers; on
            # expiry proceed out of order (interleaved prompts beat starvation). ``>=`` (not
            # ``==``) releases every skipped worker at once; abandoned short-circuits.
            in_order = self._condition.wait_for(
                lambda: self._next_order >= order or self.abandoned.is_set(), timeout=self._timeout,
            )
            if self.abandoned.is_set():
                return False  # the turn already synthesized this result; don't advance
            if not in_order:
                logger.warning(
                    "start-order gate timed out for %s (order=%d next=%d); proceeding out of order",
                    tool_name or "tool", order, self._next_order,
                )
            try:
                if callback is not None:
                    callback()
            finally:
                self._next_order = max(self._next_order, order + 1)
                self._condition.notify_all()
        return True


class _WorkerStartOnce:
    """One worker's handle on the start-order gate: advances at most once, raising
    ``_BatchAbandoned`` (instead of dispatching late) when the batch was abandoned."""

    def __init__(self, gate: _StartOrderGate, order: int, tool_name: str) -> None:
        self._gate, self._order, self._tool_name, self._advanced = gate, order, tool_name, False

    def advance(self, callback=None) -> None:
        if self._advanced:
            return
        self._advanced = True
        if not self._gate.begin_in_order(self._order, callback, tool_name=self._tool_name):
            raise _BatchAbandoned(self._tool_name)


class _ConcurrentBatch:
    """Shared state of one concurrent tool batch: per-slot results, the start-order and
    authorization gates, and the deadline bookkeeping the wait loop needs."""

    def __init__(self, agent, messages: list, effective_task_id: str, parsed_calls: list[_ParsedCall], timeout_s: float | None) -> None:
        self.agent = agent
        self.messages = messages
        self.effective_task_id = effective_task_id
        self.parsed_calls = parsed_calls
        self.timeout_s = timeout_s
        self.results: list[Optional[_ToolOutcome]] = [None] * len(parsed_calls)
        for i, pc in enumerate(parsed_calls):
            if pc.parse_error is not None:
                self.results[i] = _ToolOutcome(pc.ref(effective_task_id), pc.parse_error, 0.0, True, True)
        self.gate = _StartOrderGate(_start_order_gate_timeout(timeout_s))
        self.authorization_gate = _ConcurrentToolAuthorizationGate()
        self.timed_out_indices: set[int] = set()

    def _dispatch_worker(self, index: int, ref: _ToolCallRef, scope_block, start_gate: _WorkerStartOnce) -> Optional[_ToolOutcome]:
        """Run one call through the middleware and synthesize its slot outcome; ``None`` when
        abandoned at the gate (the main thread already wrote this slot; emitting would
        double-report the tool_call_id)."""
        agent = self.agent
        # Approval/sudo callbacks (thread-local) and the agent turn's ContextVars are propagated by
        # propagate_context_to_thread() at the submit site below (GHSA-qg5c-hvr5-hjgr, #13617).
        start = time.time()
        blocked = dispatched = False
        try:
            import model_tools
            managed = _run_agent_tool_execution_middleware(
                agent,
                **ref.middleware_kwargs(),
                execute=lambda next_args: model_tools.handle_function_call(
                    ref.name, next_args, ref.task_id, tool_call_id=ref.call_id,
                    session_id=agent.session_id or "",
                    skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
                    skip_tool_execution_middleware=True,
                ),
                scope_block=scope_block,
                display_index=index + 1,
                begin_execution=start_gate.advance,
                authorization_gate=self.authorization_gate,
            )
            result, ref.args, ref.trace = managed.result, managed.args, managed.middleware_trace
            blocked, dispatched = managed.blocked, managed.dispatched
        except _BatchAbandoned:
            logger.info("tool %s abandoned at start-order gate; skipping dispatch", ref.name)
            return None
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
        if not blocked and not dispatched:
            ref.emit_post(agent, result, duration_ms=int(duration * 1000))
        is_error, _ = _detect_tool_failure(ref.name, result)
        if is_error:
            logger.info("tool %s failed (%.2fs): %s", ref.name, duration, str(result)[:200])
        else:
            result_chars = len(result) if isinstance(result, str) else len(str(result))
            logger.info(
                "tool %s completed (%.2fs, %d chars)", ref.name, duration, result_chars
            )
        return _ToolOutcome(ref, result, duration, is_error, blocked)

    def run_worker(self, index: int, start_order: int) -> None:
        """Worker function executed in a thread."""
        agent, pc = self.agent, self.parsed_calls[index]
        with _registered_tool_worker(agent) as _worker_tid:
            # An interrupt may have fanned out before our registration; apply it to our tid.
            if agent._interrupt_requested:
                _interrupt_worker_tids(agent, [_worker_tid], reason=getattr(agent, "_tool_interrupt_reason", None))
            _set_worker_activity_callback(agent)
            start_gate = _WorkerStartOnce(self.gate, start_order, pc.name)
            try:
                outcome = self._dispatch_worker(index, pc.ref(self.effective_task_id), pc.scope_block, start_gate)
                if outcome is not None:
                    self.results[index] = outcome
            finally:
                with contextlib.suppress(_BatchAbandoned):
                    start_gate.advance()  # keep later-ordered workers moving

    def submit_all(self, executor, runnable: list[int]) -> tuple[list, dict]:
        """Submit every runnable slot; on interpreter shutdown, synthesize error results
        for the unsubmitted remainder instead of raising. ``propagate_context_to_thread``
        carries turn ContextVars and thread-local approval/sudo callbacks into the worker."""
        futures = []
        future_to_index = {}
        for submit_index, i in enumerate(runnable):
            try:
                f = executor.submit(propagate_context_to_thread(self.run_worker), i, submit_index)
            except RuntimeError as submit_error:
                if not _is_interpreter_shutdown_submit_error(submit_error):
                    raise
                skipped = runnable[submit_index:]
                logger.warning(
                    "interpreter shutdown while scheduling concurrent tools; skipping %d unsubmitted tool(s)", len(skipped),
                )
                for skipped_i in skipped:
                    ref = self.parsed_calls[skipped_i].ref(self.effective_task_id)
                    if self.results[skipped_i] is None:
                        result = f"Error executing tool '{ref.name}': Python interpreter is shutting down; tool was not started"
                        self.results[skipped_i] = _ToolOutcome(ref, result, 0.0, True, False)
                break
            futures.append(f)
            future_to_index[f] = i
        return futures, future_to_index

    def _running_names(self, not_done, future_to_index) -> list[str]:
        return [self.parsed_calls[future_to_index[f]].name for f in not_done if f in future_to_index]

    def await_completion(self, futures, future_to_index, deadline: float | None) -> bool:
        """Wait with periodic heartbeats and interrupt checks; True when the batch was
        abandoned (deadline or interrupt) and the executor must not join its workers."""
        agent = self.agent
        _conc_start = time.time()
        while True:
            wait_timeout = 5.0
            if deadline is not None:
                remaining = deadline + self.authorization_gate.excluded_seconds() - time.monotonic()
                if remaining <= 0:
                    not_done = {f for f in futures if not f.done()}
                else:
                    wait_timeout = min(wait_timeout, remaining)
            if deadline is None or remaining > 0:
                _done, not_done = concurrent.futures.wait(futures, timeout=wait_timeout)
            if not not_done:
                return False

            timed_out = deadline is not None and time.monotonic() >= deadline + self.authorization_gate.excluded_seconds()
            if timed_out:
                self.timed_out_indices = {future_to_index[f] for f in not_done if f in future_to_index}
                logger.warning(
                    "concurrent tool batch timed out after %.1fs; %d tool(s) still running: %s",
                    self.timeout_s,
                    len(self.timed_out_indices),
                    ", ".join(self._running_names(not_done, future_to_index)[:5]),
                )
            elif agent._interrupt_requested:
                # Tools without interrupt checks (web_search, read_file) run to
                # completion; cancel unstarted futures so we don't block on them.
                agent._vprint(
                    f"{agent.log_prefix}⚡ Interrupt: cancelling {len(not_done)} pending concurrent tool(s)",
                    force=True,
                )
            else:
                _conc_elapsed = int(time.time() - _conc_start)
                # Heartbeat every ~30s (6 × 5s poll intervals)
                if _conc_elapsed > 0 and _conc_elapsed % 30 < 6:
                    _still_running = self._running_names(not_done, future_to_index)
                    agent._touch_activity(
                        f"concurrent tools running ({_conc_elapsed}s, "
                        f"{len(not_done)} remaining: {', '.join(_still_running[:3])})"
                    )
                continue
            for f in not_done:
                f.cancel()
            # Release gate-parked workers BEFORE interrupt fan-out so none later
            # dispatches a tool the turn already reported as timed out / interrupted.
            self.gate.abandon()
            if timed_out:
                with agent._tool_worker_threads_lock:
                    worker_tids = list(agent._tool_worker_threads)
                _interrupt_worker_tids(agent, worker_tids)
            else:
                # Give running tools a moment to notice the per-thread interrupt and exit gracefully.
                concurrent.futures.wait(not_done, timeout=3.0)
            return True

    def run(self) -> None:
        """Dispatch the runnable calls on a daemon pool and wait for the batch."""
        runnable = [i for i, pc in enumerate(self.parsed_calls) if pc.parse_error is None]
        if not runnable:
            return
        deadline = time.monotonic() + self.timeout_s if self.timeout_s is not None else None
        max_workers = _max_workers_for_tool_batch([(i, None, self.parsed_calls[i].name) for i in runnable])
        # Daemon workers: the stdlib pool's atexit join would let one wedged tool block exit.
        from tools.daemon_pool import DaemonThreadPoolExecutor
        executor = DaemonThreadPoolExecutor(max_workers=max_workers)
        abandon_executor = False
        try:
            futures, future_to_index = self.submit_all(executor, runnable)
            abandon_executor = self.await_completion(futures, future_to_index, deadline)
        finally:
            # Every abandoning exit releases gate-parked workers and leaves wedged threads
            # detached rather than joining them; normal completion joins.
            if abandon_executor:
                self.gate.abandon()
            executor.shutdown(wait=not abandon_executor, cancel_futures=abandon_executor)




def _append_batch_results(agent, messages: list, effective_task_id: str, batch: _ConcurrentBatch, budget: BudgetConfig) -> bool:
    """Append every slot's result in original call order; returns False at the first
    failed flush (the caller must stop the batch)."""
    for i, pc in enumerate(batch.parsed_calls):
        r = batch.results[i]
        # A worker may finish between the deadline snapshot and this loop;
        # prefer its real result over a fabricated timeout.
        if r is None:
            ref, is_error, blocked = pc.ref(effective_task_id), True, False
            function_result, tool_duration, effect_disposition = _unfinished_tool_result(
                agent, ref, timed_out=i in batch.timed_out_indices, timeout_s=batch.timeout_s,
            )
        else:
            ref, function_result, tool_duration, is_error, blocked = r.ref, r.result, r.duration, r.is_error, r.blocked
            effect_disposition = "none" if blocked else None
            if pc.parse_error is not None:
                ref.emit_invalid_arguments(agent, r.result)
        committed = _commit_tool_result(
            agent, messages, ref, function_result,
            budget=budget, tool_duration=tool_duration, is_error=is_error, blocked=blocked,
            effect_disposition=effect_disposition, observed=r is not None,
            error_preview=lambda res: _multimodal_text_summary(res)[:200],
        )
        if committed is None:
            return False
        _persisted, display_function_result, risk_metadata = committed

        if agent._should_emit_quiet_tool_messages():
            cute_msg = _get_cute_tool_message_impl(ref.name, ref.args, tool_duration, result=display_function_result)
            agent._safe_print(f"  {cute_msg}")
        elif _tool_progress_enabled(agent):
            _print_tool_completed(agent, i + 1, tool_duration, _multimodal_text_summary(display_function_result))

        _emit_tool_complete_and_risk(agent, ref, display_function_result, risk_metadata, blocked)
    return True


def execute_tool_calls_concurrent(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> None:
    """Execute tool calls concurrently; results are appended in original call order.
    ``finalize=False`` skips end-of-batch budget enforcement and /steer injection (the
    segmented dispatcher owns turn-end work)."""
    tool_calls = assistant_message.tool_calls
    num_tools = len(tool_calls)
    _tool_budget = _budget_for_agent(agent)  # once per turn, not per result

    if agent._interrupt_requested:
        print(f"{agent.log_prefix}⚡ Interrupt: skipping {num_tools} tool call(s)")
        _append_skipped_tool_results(
            agent, messages, tool_calls, effective_task_id,
            content="[Tool execution cancelled — {name} was skipped due to user interrupt]",
            hook_error_type="user_interrupt",
            flush_stage="cancelled tool result",
            stop_on_flush_failure=False,
        )
        return

    parsed_calls = [_parse_tool_call(agent, tc) for tc in tool_calls]

    tool_names_str = ", ".join(pc.name for pc in parsed_calls)
    if _tool_progress_enabled(agent):
        print(f"  ⚡ Concurrent: {num_tools} tool calls — {tool_names_str}")

    # Resolved before the batch is built so the start-order gate can clamp under the deadline.
    timeout_s = _resolve_concurrent_tool_timeout()
    batch = _ConcurrentBatch(agent, messages, effective_task_id, parsed_calls, timeout_s)
    agent._current_tool = tool_names_str
    agent._touch_activity(f"executing {num_tools} tools concurrently: {tool_names_str}")

    spinner = _start_quiet_tool_spinner(agent, "", {}, label=f"⚡ running {num_tools} tools concurrently")
    try:
        batch.run()
    finally:
        if spinner:
            finished = [r for r in batch.results if r is not None]
            spinner.stop(f"⚡ {len(finished)}/{num_tools} tools completed in {sum(r.duration for r in finished):.1f}s total")

    if not _append_batch_results(agent, messages, effective_task_id, batch, _tool_budget):
        return
    if finalize:
        _finalize_tool_batch(agent, messages, effective_task_id, len(parsed_calls), _tool_budget)




def _start_quiet_tool_spinner(agent, function_name: str, function_args: dict, *, gate: bool = True, label: Optional[str] = None):
    """Start the quiet-mode kawaii spinner for one tool call, or return None; ``gate=False``
    skips ``_should_start_quiet_spinner`` (context-engine tools always spin)."""
    if not agent._should_emit_quiet_tool_messages() or (gate and not agent._should_start_quiet_spinner()):
        return None
    face = random.choice(KawaiiSpinner.get_waiting_faces())
    if label is None:
        display_args = _redact_tool_args_for_display(function_name, function_args) or function_args
        label = f"{_get_tool_emoji(function_name)} {_build_tool_label(function_name, display_args) or function_name}"
    spinner = KawaiiSpinner(f"{face} {label}", spinner_type='dots', print_fn=agent._print_fn)
    spinner.start()
    return spinner


def _finish_quiet_tool_spinner(agent, spinner, function_name: str, function_args: dict, tool_duration: float, result) -> None:
    """Stop the spinner with the cute completion line, or print it when no spinner ran."""
    if spinner or agent._should_emit_quiet_tool_messages():
        cute = _get_cute_tool_message_impl(function_name, function_args, tool_duration, result=result)
        spinner.stop(cute) if spinner else agent._vprint(f"  {cute}")




@dataclass
class _SequentialDispatch:
    """How one sequential call executes: the callable plus its spinner/error policy."""

    execute: Callable[[dict], Any]
    spinner: Any = None
    middleware_trace_arg: Optional[list] = None  # forwarded to the middleware runner (registry closure reads it)
    error_result: Optional[Callable[[Exception], str]] = None  # None → exceptions propagate (inline/delegate own failures)
    error_log: str = ""
    handles_keyboard_interrupt: bool = False
    is_delegate: bool = False
    finish_spinner: bool = True
    finish_in_finally: bool = True  # inline tools print their completion line only on success


def _resolve_sequential_dispatch(agent, ref: _ToolCallRef, messages: list) -> _SequentialDispatch:
    """Pick the execute callable for one sequential call and start its spinner. Precedence:
    inline agent-level tools, delegate_task, context-engine tools, memory-provider tools,
    then the registry."""
    function_name, function_args, effective_task_id, tool_call_id, middleware_trace = (
        ref.name, ref.args, ref.task_id, ref.call_id, ref.trace,
    )
    # Registry tools: post hook is owned by this executor (inner observer suppressed).
    def _execute(next_args: dict) -> Any:
        import model_tools

        with model_tools.suppress_post_tool_call_hook():
            return model_tools.handle_function_call(
                function_name,
                next_args,
                effective_task_id,
                tool_call_id=tool_call_id,
                session_id=agent.session_id or "",
                turn_id=getattr(agent, "_current_turn_id", "") or "",
                api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
                skip_pre_tool_call_hook=True,
                skip_tool_request_middleware=True,
                skip_tool_execution_middleware=True,
                tool_request_middleware_trace=list(middleware_trace),
                enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                disabled_toolsets=getattr(agent, "disabled_toolsets", None),
            )

    return _SequentialDispatch(
        execute=_execute,
        spinner=_start_quiet_tool_spinner(agent, function_name, function_args) if agent.quiet_mode else None,
        middleware_trace_arg=middleware_trace,
        error_result=lambda e: f"Error executing tool '{function_name}': {e}",
        error_log="handle_function_call raised for %s: %s",
        handles_keyboard_interrupt=True,
        finish_spinner=bool(agent.quiet_mode),
    )






def _run_sequential_call(
    agent,
    dispatch: _SequentialDispatch,
    ref: _ToolCallRef,
    *,
    scope_block: Optional[str],
    messages: list,
    remaining_calls,
    display_index: int,
    tool_start_time: float,
) -> tuple[_ManagedToolResult, float]:
    """Run one sequential call with its spinner/error policy; returns ``(managed, duration)``.
    KeyboardInterrupt (registry tools only) emits results for THIS and every remaining call
    before re-raising so the tool-call turn keeps matching results (alternation)."""
    _spinner_result = None
    try:
        managed = _run_agent_tool_execution_middleware(
            agent,
            **dict(ref.middleware_kwargs(), middleware_trace=dispatch.middleware_trace_arg),
            execute=dispatch.execute,
            scope_block=scope_block,
            display_index=display_index,
        )
        ref.args = managed.args
        _spinner_result = managed.result
    except KeyboardInterrupt:
        if not dispatch.handles_keyboard_interrupt:
            raise
        _spinner_result = ref.emit_cancelled(agent, tool_start_time)
        with contextlib.suppress(Exception):
            agent.interrupt("keyboard interrupt")
        _append_skipped_tool_results(
            agent, messages, remaining_calls, ref.task_id,
            content="[Tool execution cancelled — {name} was skipped due to keyboard interrupt]",
        )
        raise
    except Exception as tool_error:
        if dispatch.error_result is None:
            raise
        function_result = dispatch.error_result(tool_error)
        logger.error(dispatch.error_log, ref.name, tool_error, exc_info=True)
        managed = _ManagedToolResult(result=function_result, args=ref.args, middleware_trace=ref.trace, blocked=False, dispatched=False)
    finally:
        if dispatch.is_delegate:
            agent._delegate_spinner = None
        tool_duration = time.time() - tool_start_time
        if dispatch.finish_spinner and dispatch.finish_in_finally:
            _finish_quiet_tool_spinner(agent, dispatch.spinner, ref.name, ref.args, tool_duration, _spinner_result)
    if dispatch.finish_spinner and not dispatch.finish_in_finally:
        _finish_quiet_tool_spinner(agent, dispatch.spinner, ref.name, ref.args, tool_duration, _spinner_result)
    return managed, tool_duration


def _publish_sequential_result(agent, messages: list, ref: _ToolCallRef, managed: _ManagedToolResult, *, tool_duration: float, index: int, budget: BudgetConfig) -> bool:
    """Terminal hook → observe → commit → completion callbacks/print for one sequential
    result; False when the incremental flush failed (the caller must stop the batch)."""
    ref.args, ref.trace, function_result = managed.args, managed.middleware_trace, managed.result
    _execution_timed_out = isinstance(function_result, (_ToolTimeoutResult, _ToolCancelledResult))
    # Multimodal dict results (_multimodal=True) are not sliceable as strings.
    _result_len = len(function_result) if isinstance(function_result, str) else len(str(function_result))
    _is_error_result, _ = _detect_tool_failure(ref.name, function_result)
    # Inline-dispatched runtime tools never reach handle_function_call, so the
    # executor owns the one terminal post_tool_call per tool_call_id (the inner
    # observer is suppressed); also stops an abandoned timeout worker reporting late.
    if not managed.blocked and not _execution_timed_out:
        ref.emit_post(agent, function_result, duration_ms=int(tool_duration * 1000))
    committed = _commit_tool_result(
        agent, messages, ref, function_result,
        budget=budget, tool_duration=tool_duration, is_error=_is_error_result, blocked=managed.blocked,
        effect_disposition="unknown" if _execution_timed_out else None, observed=True,
        error_preview=lambda res: res[:200] if isinstance(res, str) and not agent.verbose_logging else res,
        success_log_chars=_result_len,
        verbose_text=_multimodal_text_summary,
    )
    if committed is None:
        return False
    function_result, display_function_result, risk_metadata = committed

    _emit_tool_complete_and_risk(agent, ref, display_function_result, risk_metadata, managed.blocked)
    if _tool_progress_enabled(agent):
        _print_tool_completed(agent, index, tool_duration, function_result)
    return True


def execute_tool_calls_sequential(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> None:
    _execute_tool_calls_sequential(agent, assistant_message, messages, effective_task_id, api_call_count, finalize=finalize)


def _execute_tool_calls_sequential(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> None:
    """Execute tool calls sequentially (single calls or interactive tools). ``finalize=False``
    skips end-of-batch budget enforcement and /steer injection (the segmented dispatcher
    owns turn-end work)."""
    _tool_budget = _budget_for_agent(agent)  # once per turn, not per result
    tool_calls = assistant_message.tool_calls

    for i, tool_call in enumerate(tool_calls, 1):
        if getattr(agent, "_incremental_persistence_failed", False):
            return
        # Check interrupt BEFORE each tool so a "stop" during the previous one skips the rest.
        if agent._interrupt_requested:
            if not _skip_remaining_sequential(
                agent, messages, tool_calls[i - 1:], effective_task_id,
                notice="tool call(s)",
                content="[Tool execution cancelled — {name} was skipped due to user interrupt]",
                hook_error_type="user_interrupt",
                hook_id=lambda tc: getattr(tc, "id", "") or "",
                flush_stage="cancelled tool result",
            ):
                return
            break

        pc = _parse_tool_call(agent, tool_call, flatten_probe=True)
        ref = pc.ref(effective_task_id)
        if pc.parse_error is not None:
            if not _append_invalid_arguments_result(agent, messages, ref, pc.parse_error):
                return
            continue

        tool_start_time = time.time()
        dispatch = _resolve_sequential_dispatch(agent, ref, messages)
        managed, tool_duration = _run_sequential_call(
            agent, dispatch, ref,
            scope_block=pc.scope_block,
            messages=messages,
            remaining_calls=tool_calls[i - 1:],
            display_index=i,
            tool_start_time=tool_start_time,
        )
        if not _publish_sequential_result(agent, messages, ref, managed, tool_duration=tool_duration, index=i, budget=_tool_budget):
            return

        if agent._interrupt_requested and i < len(tool_calls):
            if not _skip_remaining_sequential(
                agent, messages, tool_calls[i:], effective_task_id,
                notice="remaining tool call(s)",
                content="[Tool execution skipped — {name} was not started. User sent a new message]",
                flush_stage="skipped tool result",
            ):
                return
            break

    if finalize:
        _finalize_tool_batch(agent, messages, effective_task_id, len(tool_calls), _tool_budget)


def _image_generate_parallel_limit() -> int:
    """Configured image-generation parallelism cap (conservative: backend bursts hit rate limits)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        image_gen = cfg.get("image_gen") if isinstance(cfg, dict) else None
        value = image_gen.get("max_parallel_requests") if isinstance(image_gen, dict) else None
    except Exception:
        value = None

    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = _DEFAULT_IMAGE_PARALLEL_REQUESTS
    return max(1, min(limit, _MAX_TOOL_WORKERS))


def _is_interpreter_shutdown_submit_error(exc: RuntimeError) -> bool:
    """Shutdown-race predicate; ``tools.interpreter_shutdown`` knows both CPython message variants.

    Delegates so all sites (cron delivery, conversation-loop retry, tool submission) recognize both CPython
    shutdown-message variants instead of each matching its own substring (the bug class behind
    #55924/#58720).
    """
    from tools.interpreter_shutdown import interpreter_shutting_down

    return interpreter_shutting_down(exc)


def _append_skipped_tool_results(
    agent,
    messages: list,
    tool_calls,
    effective_task_id: str,
    *,
    content: str,
    hook_error_type: Optional[str] = None,
    hook_id: Optional[Callable[[Any], str]] = None,
    flush_stage: Optional[str] = None,
    stop_on_flush_failure: bool = True,
) -> bool:
    """Append one ``tool`` result per unstarted call so the assistant tool-call turn never
    lacks matching results (role alternation). ``content`` is formatted with ``{name}``;
    ``hook_error_type`` also emits the terminal ``post_tool_call`` (status=cancelled) per
    call with ``hook_id`` overriding the hook's id; ``flush_stage`` flushes after each
    append and returns False on the first failed flush when ``stop_on_flush_failure``."""
    for tc in tool_calls:
        name = _tc_name(tc)
        result = content.format(name=name)
        messages.append(make_tool_result_message(name, result, _pairing_tool_call_id(tc), effect_disposition="none"))
        if hook_error_type is not None:
            _ToolCallRef(name, {}, effective_task_id, (hook_id or _pairing_tool_call_id)(tc), []).emit_post(
                agent, result,
                status="cancelled", error_type=hook_error_type, error_message="Tool execution skipped due to user interrupt",
            )
    return True


def _tool_search_scoped_names(agent) -> frozenset:
    """Deferrable tool names the session may invoke via ``tool_call``; the unwrap bypasses
    the bridge's scope check in ``model_tools.handle_function_call``, so restricted sessions
    validate against this set. Cached on the agent, keyed by registry scope/generation."""
    try:
        import model_tools
        from tools import tool_search as _ts
        from tools.registry import registry as _registry
    except Exception:
        return frozenset()

    enabled = getattr(agent, "enabled_toolsets", None)
    disabled = getattr(agent, "disabled_toolsets", None)
    cache_key = (
        _registry.current_scope_key(),
        getattr(_registry, "_generation", 0),
        frozenset(enabled) if enabled is not None else None,
        frozenset(disabled) if disabled is not None else None,
    )
    cached = getattr(agent, "_tool_search_scope_cache", None)
    if cached is not None and cached[0] == cache_key:
        return cached[1]
    try:
        names = _ts.scoped_deferrable_names(model_tools.get_tool_definitions(
            enabled_toolsets=enabled, disabled_toolsets=disabled, quiet_mode=True, skip_tool_search_assembly=True,
        ) or [])
    except Exception:
        names = frozenset()
    with contextlib.suppress(Exception):
        agent._tool_search_scope_cache = (cache_key, names)
    return names


def _interrupt_worker_tids(agent, tids, *, reason=_NO_REASON) -> None:
    """Raise the interrupt bit on each worker tid (best-effort, via ``run_agent``)."""
    kwargs = {} if reason is _NO_REASON else {"reason": reason}
    for tid in tids:
        with contextlib.suppress(Exception):
            _ra()._set_interrupt(True, tid, **kwargs)


def _preview(text: str, limit: int) -> str:
    return text[:limit] + "..." if len(text) > limit else text


def _print_tool_completed(agent, index: int, tool_duration: float, result) -> None:
    """Non-quiet ``✅ Tool N completed`` line (full result under verbose logging)."""
    if agent.verbose_logging:
        print(f"  ✅ Tool {index} completed in {tool_duration:.2f}s")
        print(agent._wrap_verbose("Result: ", result))
    else:
        print(f"  ✅ Tool {index} completed in {tool_duration:.2f}s - {_preview(result if isinstance(result, str) else str(result), agent.log_prefix_chars)}")


def _unfinished_tool_result(agent, ref: _ToolCallRef, *, timed_out: bool, timeout_s: float | None) -> tuple[str, float, Optional[str]]:
    """Synthesize the result for a slot no worker filled (deadline, interrupt, or a thread
    that never returned), emit its terminal post_tool_call, and return
    ``(function_result, tool_duration, effect_disposition)``."""
    if timed_out:
        suffix = f"{timeout_s:.1f}s" if timeout_s is not None else "the configured timeout"
        function_result = f"Error executing tool '{ref.name}': timed out after {suffix}"
        outcome = dict(duration_ms=int((timeout_s or 0.0) * 1000), status="timeout", error_type="tool_timeout", error_message=function_result)
        tool_duration, effect_disposition = float(timeout_s or 0.0), "unknown"
    elif agent._interrupt_requested:
        function_result = f"[Tool execution cancelled — {ref.name} was skipped due to user interrupt]"
        outcome = dict(status="cancelled", error_type="keyboard_interrupt", error_message="Tool execution cancelled by user interrupt")
        tool_duration, effect_disposition = 0.0, None
    else:
        function_result = f"Error executing tool '{ref.name}': thread did not return a result"
        outcome = dict(status="error", error_type="thread_missing_result", error_message=function_result)
        tool_duration, effect_disposition = 0.0, None
    ref.emit_post(agent, function_result, **outcome)
    return function_result, tool_duration, effect_disposition


def _skip_remaining_sequential(agent, messages: list, remaining, effective_task_id: str, *, notice: str, **skip_kwargs) -> bool:
    """Announce an interrupt and append one skipped result per unstarted call; False when
    a flush failed (the caller must stop the batch)."""
    agent._vprint(f"{agent.log_prefix}⚡ Interrupt: skipping {len(remaining)} {notice}", force=True)
    return _append_skipped_tool_results(agent, messages, remaining, effective_task_id, **skip_kwargs)


def _append_invalid_arguments_result(agent, messages: list, ref: _ToolCallRef, parse_error: str) -> bool:
    """Emit + append the parse-error result for a call whose arguments were not a JSON object."""
    ref.emit_invalid_arguments(agent, parse_error)
    messages.append(make_tool_result_message(ref.name, parse_error, ref.call_id))
    return True


def _tc_name(tool_call: Any) -> str:
    return getattr(getattr(tool_call, "function", None), "name", "") or "tool"
