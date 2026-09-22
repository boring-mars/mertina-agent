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
from pathlib import Path
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

# 原实现保留：以下 Hermes 外部依赖只服务旧 middleware、UI、持久化和动态分段路径。
# from agent.display import (
#     KawaiiSpinner,
#     build_tool_preview as _build_tool_preview,
#     build_tool_label as _build_tool_label,
#     get_cute_tool_message as _get_cute_tool_message_impl,
#     get_tool_emoji as _get_tool_emoji,
#     redact_tool_args_for_display as _redact_tool_args_for_display,
#     _detect_tool_failure,
# )
# from agent.message_sanitization import coalesce_tool_call_id
# from agent.inline_tool_executors import (
#     INLINE_TOOL_EXECUTORS,
#     InlineToolContext,
#     emit_terminal_post_tool_call,
#     tool_hook_ids,
# )
# from agent.tool_dispatch_helpers import (
#     _NEVER_PARALLEL_TOOLS,
#     _is_destructive_command,
#     _is_multimodal_tool_result,
#     _multimodal_text_summary,
#     _append_subdir_hint_to_multimodal,
#     _plan_tool_batch_segments,
#     make_tool_result_message,
# )
# from tools.terminal_tool_lifecycle import get_active_env
# from tools.thread_context import propagate_context_to_thread
# from tools.tool_result_storage import (
#     maybe_persist_tool_result,
#     enforce_turn_budget,
#     extract_persisted_path,
# )
# from tools.budget_config import BudgetConfig, DEFAULT_BUDGET, budget_for_context_window

logger = logging.getLogger(__name__)


#  _pairing_tool_call_id = coalesce_tool_call_id  # canonical id used by the persisted assistant message
# [改动] B：当前最小 executor 直接读取 assistant message 的原始调用 ID，不再依赖 Hermes sanitizer。
def _pairing_tool_call_id(tool_call: Any) -> str:
    """[改动] 取得与 assistant tool call 一致的结果配对 ID。"""
    return str(getattr(tool_call, "id", None) or getattr(tool_call, "tool_call_id", "") or "")


def _tc_name(tool_call: Any) -> str:
    return getattr(getattr(tool_call, "function", None), "name", "") or "tool"


# Mertina v0.1 暂不保留：结果落盘引用和文件修改检查点。
# def _record_persisted_path_for_stub(agent, tool_call_id: str, function_result) -> None:
#     """Record the spillover file path so a later result-reference stub can't dangle (best-effort)."""
#     try:
#         candidates = [function_result] if isinstance(function_result, str) else [
#             function_result.get("text_summary"),
#             *(p.get("text") for p in function_result.get("content") or [] if isinstance(p, dict)),
#         ] if _is_multimodal_tool_result(function_result) else []
#         path = next((p for p in map(extract_persisted_path, candidates) if p), None)
#         if path:
#             agent._tool_guardrails.record_persisted_result(tool_call_id, path)
#     except Exception as exc:
#         logger.debug("persisted-path record for result stub failed: %s", exc)
#
#
# def _ensure_file_checkpoint(agent, function_name: str, function_args: dict, effective_task_id: str) -> None:
#     """Checkpoint the same workspace path that the file tool will mutate, resolved the way
#     file tools do (against the task's live cwd, which differs from the process cwd in Docker)."""
#     file_path = function_args.get("path", "")
#     if not file_path:
#         return
#     from agent.file_safety import is_nt_namespace_path
#     from tools.file_tools_paths import _resolve_path_for_task, container_backend_for_task
#
#     if container_backend_for_task(effective_task_id or "default") is not None:
#         return  # container paths: nothing to checkpoint on the host
#
#     # Resolving an NT-namespace path is itself the NTLM-leak trigger; leave the
#     # tool's raw-string guard to refuse it without a checkpoint stat.
#     if is_nt_namespace_path(file_path):
#         return
#     resolved_path = _resolve_path_for_task(file_path, effective_task_id or "default")
#     agent._checkpoint_mgr.ensure_checkpoint(
#         agent._checkpoint_mgr.get_working_dir_for_path(str(resolved_path)), f"before {function_name}",
#     )


#  def _budget_for_agent(agent) -> BudgetConfig:
#      """Tool-result BudgetConfig scaled to the agent's context window. Unknown length goes
#      through ``budget_for_context_window(None)`` (not DEFAULT_BUDGET) so the MCP threshold
#      override still applies.
#
#      Large-context models keep the historical 100K/200K char defaults; small models (e.g. a 65K-token local
#      model switched into mid-session) get a budget proportional to their window so a single large tool result
#      can't push the request past the model's limit (#23767). Falls back to the default budget when the
#      context length isn't resolvable.
#      """
#      try:
#          ctx = getattr(getattr(agent, "context_compressor", None), "context_length", None)
#          return budget_for_context_window(int(ctx) if ctx else None)
#      except Exception:
#          return DEFAULT_BUDGET

_MAX_TOOL_WORKERS = 8  # concurrent worker threads per batch
_DEFAULT_IMAGE_PARALLEL_REQUESTS = 4
# Generous: slow-but-valid tool work must never be preempted by the batch guard.
_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S = 420.0
# Long enough for an approval round-trip, short enough that one wedged dispatch can't starve the batch.
_START_ORDER_GATE_TIMEOUT_S = 120.0
# Fallback only; the effective bound derives from approvals.timeout (_authorization_gate_lock_timeout).
_AUTHORIZATION_GATE_LOCK_TIMEOUT_S = 360.0


# Mertina v0.1 暂不保留：人工审批锁和批次放弃控制。
# def _authorization_gate_lock_timeout() -> float:
#     """Authorization-lock bound = ``tools.approval_human_wait.human_wait_ceiling`` (approval timeout +
#     margin, capped so it can't overflow Lock.acquire): never break serialization while a
#     prompt is answerable, never let a wedged holder park workers forever. Deliberately NOT
#     min()'d with the fallback so the gate never gives up early.
#
#     Delegates to ``tools.approval_human_wait.human_wait_ceiling`` — the same bound that clamps a human-wait window's
#     deadline contribution — so the two can't drift. Long enough that serialization is never broken while a
#     legitimate approval prompt is still answerable; short enough that a wedged holder (hanging
#     ``pre_tool_call`` plugin, dead approval client) cannot park other workers forever (#79719). Resolved
#     once per gate (per batch), so a mid-process ``approvals.timeout`` change applies from the next batch.
#     """
#     try:
#         from tools.approval_human_wait import human_wait_ceiling
#
#         # human_wait_ceiling is platform-safety-capped (agent/deadline.py MAX_SAFE_TIMEOUT_S): a huge
#         # approvals.timeout can no longer overflow Lock.acquire's time_t on macOS (#83220). Deliberately NOT
#         # min()'d with _AUTHORIZATION_GATE_LOCK_TIMEOUT_S — the gate must never give up while a legitimate
#         # approval prompt is still answerable (#79719), so a configured approvals.timeout above 360s must
#         # extend the gate.
#         return human_wait_ceiling()
#     except Exception:
#         return _AUTHORIZATION_GATE_LOCK_TIMEOUT_S
#
#
# class _BatchAbandoned(BaseException):
#     """Raised inside a worker when the batch was abandoned before dispatch; a BaseException
#     so ``except Exception`` handlers in the middleware chain can't swallow it."""


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


#  def _resolve_concurrent_tool_timeout() -> float | None:
#      """Per-batch concurrent deadline: ``timeouts.tools.concurrent_batch`` wins,
#      ``HERMES_CONCURRENT_TOOL_TIMEOUT_S`` is the legacy bridge, ``0``/negative disables."""
#      from agent.deadline import resolve_timeout
#
#      return resolve_timeout(
#          "tools.concurrent_batch",
#          default=_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S,
#          env_var="HERMES_CONCURRENT_TOOL_TIMEOUT_S",
#      )


# [改动] A：并发批次只保留固定超时，不再读取 Hermes 动态配置。
def _resolve_concurrent_tool_timeout() -> float | None:
    """Mertina v0.1 的并发批次使用固定超时；复杂配置兼容留待后续阶段。"""
    return _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S


# Mertina v0.1 暂不保留：每个工具完成后的会话数据库增量持久化和图片工具特例。
# def _flush_session_db_after_tool_progress(agent, messages: list, *, stage: str) -> bool:
#     """Flush tool-call progress to the session DB before projecting it to any UI: tool side
#     effects can kill/restart the process before turn-end persistence runs."""
#     from agent.conversation_loop import _maybe_inject_run_budget_wrapup
#     from agent.turn_iteration_prep import _maybe_inject_iteration_budget_warning
#
#     # Persist exactly the checkpoint text the next model call will see, before stamping
#     # this tool result as durable. Already-written rows must never be rewritten later.
#     _maybe_inject_run_budget_wrapup(agent, messages)
#     _maybe_inject_iteration_budget_warning(agent, messages)
#     try:
#         persisted = agent._flush_messages_to_session_db(messages) is not False
#         if not persisted:
#             agent._incremental_persistence_failed = True
#             # The flush recorded any classified cause; default to 'unknown' only if nothing more specific exists.
#             if getattr(agent, "_last_persistence_error_cause", None) is None:
#                 agent._last_persistence_error_cause = "unknown"
#         return persisted
#     except Exception as exc:
#         agent._incremental_persistence_failed = True
#         from hermes_state import classify_persistence_error
#         agent._last_persistence_error_cause = classify_persistence_error(exc)
#         logger.warning("Incremental tool-call persistence failed after %s: %s", stage, exc)
#         return False
#
#
# def _image_generate_parallel_limit() -> int:
#     """Configured image-generation parallelism cap (conservative: backend bursts hit rate limits)."""
#     try:
#         from hermes_cli.config import load_config
#
#         cfg = load_config() or {}
#         image_gen = cfg.get("image_gen") if isinstance(cfg, dict) else None
#         value = image_gen.get("max_parallel_requests") if isinstance(image_gen, dict) else None
#     except Exception:
#         value = None
#
#     try:
#         limit = int(value)
#     except (TypeError, ValueError):
#         limit = _DEFAULT_IMAGE_PARALLEL_REQUESTS
#     return max(1, min(limit, _MAX_TOOL_WORKERS))


#  def _max_workers_for_tool_batch(runnable_calls) -> int:
#      """Return the worker cap for a concurrent tool batch."""
#      if not runnable_calls:
#          return 0
#      max_workers = _MAX_TOOL_WORKERS
#      if any((call[2] if len(call) >= 3 else None) == "image_generate" for call in runnable_calls):
#          max_workers = min(max_workers, _image_generate_parallel_limit())
#      return min(len(runnable_calls), max_workers)


# [改动] A：worker 数量只按可执行调用数量计算，不再应用图片工具特例。
def _max_workers_for_tool_batch(runnable_calls) -> int:
    """返回独立工具批次的 worker 上限。"""
    if not runnable_calls:
        return 0
    return min(len(runnable_calls), _MAX_TOOL_WORKERS)


# Mertina v0.1 暂不保留：旧入口兼容和解释器关闭期间的线程池兼容。
# def _ra():
#     """Lazy reference to ``run_agent`` so patches like ``run_agent._set_interrupt`` work."""
#     import run_agent
#     return run_agent
#
#
# def _is_interpreter_shutdown_submit_error(exc: RuntimeError) -> bool:
#     """Shutdown-race predicate; ``tools.interpreter_shutdown`` knows both CPython message variants.
#
#     Delegates so all sites (cron delivery, conversation-loop retry, tool submission) recognize both CPython
#     shutdown-message variants instead of each matching its own substring (the bug class behind
#     #55924/#58720).
#     """
#     from tools.interpreter_shutdown import interpreter_shutting_down
#
#     return interpreter_shutting_down(exc)


#  _emit_terminal_post_tool_call = emit_terminal_post_tool_call


#  @dataclass
#  class _ToolCallRef:
#      """Identity of one tool call as every hook / result message sees it: the (possibly
#      middleware-rewritten) name and args, the task, the pairing id and the request trace."""
#
#      name: str
#      args: dict
#      task_id: str
#      call_id: str
#      trace: list
#
#      def middleware_kwargs(self) -> dict[str, Any]:
#          """Keyword form ``_run_agent_tool_execution_middleware`` (and tests patching it) expect."""
#          return {
#              "function_name": self.name, "function_args": self.args, "effective_task_id": self.task_id,
#              "tool_call_id": self.call_id, "middleware_trace": self.trace,
#          }
#
#      def emit_post(self, agent, result, *, trace=None, **outcome) -> None:
#          """Emit the one terminal ``post_tool_call`` for this call (``outcome`` = status /
#          error_type / error_message / duration_ms). Resolved through the module attribute so
#          tests patching ``_emit_terminal_post_tool_call`` still intercept."""
#          _emit_terminal_post_tool_call(
#              agent,
#              function_name=self.name,
#              function_args=self.args,
#              result=result,
#              effective_task_id=self.task_id,
#              tool_call_id=self.call_id,
#              middleware_trace=list(self.trace if trace is None else trace),
#              **outcome,
#          )
#
#      def emit_cancelled(self, agent, start_time: float) -> str:
#          """Synthesize the ``cancelled`` result for a KeyboardInterrupt mid-tool and emit its hook."""
#          message = "Tool execution cancelled by user interrupt"
#          result = json.dumps({"error": message, "status": "cancelled"}, ensure_ascii=False)
#          self.emit_post(
#              agent, result, duration_ms=int((time.time() - start_time) * 1000),
#              status="cancelled", error_type="keyboard_interrupt", error_message=message,
#          )
#          return result
#
#      def emit_invalid_arguments(self, agent, result: str) -> None:
#          self.emit_post(
#              agent, result, trace=[],
#              status="error", error_type="invalid_tool_arguments", error_message="Tool arguments must be a valid JSON object",
#          )


#  def _append_skipped_tool_results(
#      agent,
#      messages: list,
#      tool_calls,
#      effective_task_id: str,
#      *,
#      content: str,
#  ) -> bool:
#      """为每个未启动的工具调用补充一个结果消息，保持消息历史完整。"""
#      for tc in tool_calls:
#          name = _tc_name(tc)
#          result = content.format(name=name)
#          messages.append(make_tool_result_message(name, result, _pairing_tool_call_id(tc), effect_disposition="none"))
#      return True


# Mertina v0.1 暂不保留：动态 tool search、scope 缓存和旧工具名称兼容。
# def _tool_search_scoped_names(agent) -> frozenset:
#     """Deferrable tool names the session may invoke via ``tool_call``; the unwrap bypasses
#     the bridge's scope check in ``model_tools.handle_function_call``, so restricted sessions
#     validate against this set. Cached on the agent, keyed by registry scope/generation."""
#     try:
#         import model_tools
#         from tools import tool_search as _ts
#         from tools.registry import registry as _registry
#     except Exception:
#         return frozenset()
#
#     enabled = getattr(agent, "enabled_toolsets", None)
#     disabled = getattr(agent, "disabled_toolsets", None)
#     cache_key = (
#         _registry.current_scope_key(),
#         getattr(_registry, "_generation", 0),
#         frozenset(enabled) if enabled is not None else None,
#         frozenset(disabled) if disabled is not None else None,
#     )
#     cached = getattr(agent, "_tool_search_scope_cache", None)
#     if cached is not None and cached[0] == cache_key:
#         return cached[1]
#     try:
#         names = _ts.scoped_deferrable_names(model_tools.get_tool_definitions(
#             enabled_toolsets=enabled, disabled_toolsets=disabled, quiet_mode=True, skip_tool_search_assembly=True,
#         ) or [])
#     except Exception:
#         names = frozenset()
#     with contextlib.suppress(Exception):
#         agent._tool_search_scope_cache = (cache_key, names)
#     return names
#
#
# def _canonical_tool_name(function_name: str) -> str:
#     """Map legacy tool-name aliases BEFORE agent-loop dispatch."""
#     from model_tools import _LEGACY_TOOL_ALIASES as _lta
#
#     return _lta.get(function_name, function_name)
#
#
# def _unwrap_tool_search_call(
#     agent, function_name: str, function_args: dict, *, flatten_probe: bool = False
# ) -> tuple[str, dict, Optional[str]]:
#     """Peel the ``tool_call`` bridge so downstream hooks (checkpointing, guardrails, plugin
#     hooks, activity feed) see the underlying tool; ``tool_call.function`` stays untouched for
#     the transcript and tool_call_id pairing.
#
#     The unwrap bypasses handle_function_call's scope check, so session toolset scope is
#     enforced HERE. Returns ``(name, args, scope_block)``; ``scope_block`` is the block
#     message when the underlying tool is out of scope or its args fail the deferred-schema
#     probe (``flatten_probe`` collapses the probe's JSON payload to one plain string for
#     callers that wrap the message in ``{"error": ...}``).
#     """
#     scope_block: Optional[str] = None
#     try:
#         from tools import tool_search as _ts
#         if function_name != _ts.TOOL_CALL_NAME:
#             return function_name, function_args, None
#         underlying, underlying_args, err = _ts.resolve_underlying_call(function_args)
#         if err or not underlying:
#             return function_name, function_args, None
#         if underlying == _ts.CONNECTOR_BATCH_SENTINEL:
#             # Both executors retain the wrapper: scope/probe/hooks run per entry
#             # in the batch dispatcher, not against a synthetic registry name.
#             return function_name, function_args, None
#         if underlying not in _tool_search_scoped_names(agent):
#             return function_name, function_args, (
#                 f"'{underlying}' is not available in this session. Use tool_search to find tools you can call."
#             )
#         # Validate before unwrapping: the generic bridge hides the concrete
#         # parameter schema from provider-native tool-call validation.
#         scope_block = _ts.validate_deferred_call_args(underlying, underlying_args)
#         if scope_block is None:
#             return underlying, underlying_args, None
#         if flatten_probe:
#             probe = json.loads(scope_block)
#             scope_block = (
#                 f"{probe.get('error', '')} Parameters schema: "
#                 f"{json.dumps(probe.get('parameters', {}), ensure_ascii=False)}. "
#                 f"{probe.get('hint', '')}"
#             ).strip()
#     except Exception:
#         pass
#     return function_name, function_args, scope_block


#  @dataclass
#  class _ParsedCall:
#      """One model tool call after alias canonicalization, arg parsing and bridge unwrap."""
#
#      tool_call: Any
#      name: str
#      args: dict
#      middleware_trace: list
#      parse_error: Optional[str]
#      scope_block: Optional[str]
#
#      def ref(self, task_id: str) -> _ToolCallRef:
#          return _ToolCallRef(self.name, self.args, task_id, _pairing_tool_call_id(self.tool_call), self.middleware_trace)


#  def _parse_tool_call(agent, tool_call, *, flatten_probe: bool = False) -> _ParsedCall:
#      name = _canonical_tool_name(tool_call.function.name)
#      args, parse_error = _parse_tool_arguments(tool_call.function.arguments)
#      scope_block = None
#      if parse_error is None:
#          name, args, scope_block = _unwrap_tool_search_call(agent, name, args, flatten_probe=flatten_probe)
#      return _ParsedCall(tool_call, name, args, [], parse_error, scope_block)


#  @dataclass
#  class _ManagedToolResult:
#      result: Any
#      args: dict[str, Any]
#      middleware_trace: list[dict[str, Any]]
#      blocked: bool
#      dispatched: bool


#  class _ToolTimeoutResult(str):
#      """Marker for a synthesized sequential-tool timeout result."""


#  class _ToolCancelledResult(str):
#      """Marker for a synthesized sequential-tool user-interrupt result; its terminal
#      post_tool_call was already emitted, so a late-finishing abandoned worker must not report."""


# Mertina v0.1 暂不保留：并发人工审批 gate。
# class _ConcurrentToolAuthorizationGate:
#     """Serialize policy prompts and exclude human approval waits from batch deadlines.
#
#     The acquire is BOUNDED: on expiry the worker prompts unserialized rather than starving
#     the batch behind a wedged plugin/approval client. Exclusion is measured at the SOURCE
#     of the human wait (``tools.approval.human_wait_seconds``), NOT as gate residency —
#     residency-based exclusion let a wedged plugin keep the deadline from ever firing.
#
#     Serialization keeps concurrent approval prompts from interleaving on the user's screen. The acquire is
#     BOUNDED: a worker wedged inside the gate (a hanging ``pre_tool_call`` plugin, or an approval round-trip
#     to a client that went away) must not park every other worker forever. On expiry the worker runs its
#     prompt unserialized — worst case is interleaved prompts, strictly better than permanent starvation (same
#     tradeoff as the start-order gate, #79705).
#     Gate residency is arbitrary code — using it as the exclusion signal let a wedged plugin grow the
#     exclusion 1:1 with wall clock, keeping the batch deadline's ``remaining`` constant so it never fired and
#     the turn hung forever (#79719). A wedged plugin now contributes nothing to the exclusion and the batch
#     times out normally, while a genuine approval wait (which can legitimately exceed any fixed bound) is
#     still excluded in full.
#     """
#
#     def __init__(self, *, lock_timeout: float | None = None, session_key: str | None = None) -> None:
#         self._serialization_lock = threading.Lock()
#         self._lock_timeout = _authorization_gate_lock_timeout() if lock_timeout is None else lock_timeout
#         self._session_key = session_key
#         if self._session_key is None:
#             # Snapshot on the SUBMITTING thread: excluded_seconds() is polled from the
#             # batch wait loop, whose context may differ from the workers'.
#             try:
#                 from tools.approval_context import get_current_session_key
#
#                 self._session_key = get_current_session_key()
#             except Exception:
#                 logger.debug(
#                     "authorization gate could not snapshot the session key; "
#                     "human-wait exclusion will re-resolve it at poll time",
#                     exc_info=True,
#                 )
#         self._baseline_wait_seconds = self._human_wait_seconds()
#
#     def _human_wait_seconds(self) -> float:
#         try:
#             from tools.approval_human_wait import human_wait_seconds
#
#             return human_wait_seconds(self._session_key)
#         except Exception:
#             return 0.0
#
#     def run(self, callback):
#         if not self._serialization_lock.acquire(timeout=self._lock_timeout):
#             # Deterministic failure (bad command, non-MCP URL, 401/403): every retry hits the same wall.
#             # Park immediately instead of burning the retry ladder and spamming N identical warnings
#             # (#65673). Auth failures park here too rather than returning. Returning ends the run task, and
#             # with it the only listener on ``_reconnect_event`` — so a 401 on the very first connect left
#             # the server unrevivable for the life of the process, even after the user re-authenticated with
#             # ``hermes mcp login``. Parking keeps the task alive so the 300s self-probe (and an explicit
#             # /mcp refresh) can pick up fresh tokens.
#             logger.warning(
#                 "authorization gate lock not acquired after %.1fs "
#                 "(holder wedged in a pre_tool_call plugin or approval "
#                 "round-trip?); running prompt unserialized",
#                 self._lock_timeout,
#             )
#             return callback()
#         try:
#             return callback()
#         finally:
#             self._serialization_lock.release()
#
#     def excluded_seconds(self) -> float:
#         """Return human-approval wait seconds accrued since the batch started."""
#         return max(0.0, self._human_wait_seconds() - self._baseline_wait_seconds)


# Mertina v0.1 暂不保留：worker 注册、线程中断登记和工具心跳。
# @contextlib.contextmanager
# def _registered_tool_worker(agent):
#     """Track this worker tid for interrupt fan-out (``AIAgent.interrupt()``); on ANY exit
#     (incl. BaseException) discard it and clear its interrupt bit so a recycled tid starts clean."""
#     tid = threading.current_thread().ident
#     with agent._tool_worker_threads_lock:
#         agent._tool_worker_threads.add(tid)
#     try:
#         yield tid
#     finally:
#         with agent._tool_worker_threads_lock:
#             agent._tool_worker_threads.discard(tid)
#         with contextlib.suppress(Exception):
#             _ra()._set_interrupt(False, tid)
#
#
# _NO_REASON = object()
#
#
# def _interrupt_worker_tids(agent, tids, *, reason=_NO_REASON) -> None:
#     """Raise the interrupt bit on each worker tid (best-effort, via ``run_agent``)."""
#     kwargs = {} if reason is _NO_REASON else {"reason": reason}
#     for tid in tids:
#         with contextlib.suppress(Exception):
#             _ra()._set_interrupt(True, tid, **kwargs)
#
#
# def _set_worker_activity_callback(agent) -> None:
#     """The activity callback is thread-local: bind it on THIS thread so tool-layer heartbeats fire."""
#     with contextlib.suppress(Exception):
#         from tools.environments.base import set_activity_callback
#
#         set_activity_callback(agent._touch_activity)
#
#
# # Must stay far below the gateway turn-inactivity timeout (default 1800s) so a silent tool never looks idle.
# _TOOL_ACTIVITY_HEARTBEAT_INTERVAL_S = 30.0
#
#
# def _run_tool_activity_heartbeat(
#     agent,
#     stop_event: threading.Event,
#     label: str,
#     interval: float = _TOOL_ACTIVITY_HEARTBEAT_INTERVAL_S,
#     worker_tid: int | None = None,
# ) -> None:
#     """Daemon thread stamping ``agent._touch_activity`` every ``interval`` seconds until
#     ``stop_event`` is set, so the gateway inactivity watchdog never abandons a turn whose
#     tool runs silently. Wedged tools stay bounded by the tool layer's own timeouts and by the
#     executor deadline — but a worker the executor gave up on never reaches its ``stop_event``,
#     so the heartbeat also exits once ``worker_tid`` carries the interrupt bit the abandoning
#     executor raises (``_interrupt_worker_tids``). Otherwise a tool wedged in a kernel probe
#     keeps reporting "activity" for the rest of the run and the inactivity watchdog, the second
#     line of defense, can never fire (#111922)."""
#     from tools.interrupt import is_thread_interrupted
#
#     try:
#         while not stop_event.wait(interval):
#             if is_thread_interrupted(worker_tid):
#                 return
#             agent._touch_activity(label)
#     except Exception:
#         pass  # a heartbeat must never break the agent loop
#
#
# def _run_with_activity_heartbeat(agent, function_name: str, fn):
#     """Run ``fn()`` under the activity heartbeat; covers both executor paths."""
#     stop = threading.Event()
#     thread = threading.Thread(
#         # Keep the gateway turn-inactivity watchdog from abandoning a turn whose tool call runs silently for
#         # longer than the inactivity timeout (#84491): stamp activity periodically while the tool is in
#         # flight, not just at start/completion. Both the sequential and the concurrent paths funnel through
#         # here, so a single heartbeat covers every tool.
#         target=_run_tool_activity_heartbeat,
#         args=(agent, stop, f"tool running: {function_name}"),
#         kwargs={"interval": _TOOL_ACTIVITY_HEARTBEAT_INTERVAL_S, "worker_tid": threading.current_thread().ident},
#         daemon=True,
#         name=f"tool-activity-hb-{function_name[:24]}",
#     )
#     thread.start()
#     try:
#         return fn()
#     finally:
#         stop.set()
#         thread.join(timeout=2.0)


# Mertina v0.1 暂不保留：插件 pre-hook、guardrail 和复杂阻断结果。
# def _blocked_tool_result(agent, ref: _ToolCallRef, *, block_message: Optional[str], block_error_type: str, guardrail_decision) -> str:
#     """Synthesize the result for a call blocked by scope/plugin (``block_message``) or by
#     guardrail policy (``guardrail_decision``) and emit its terminal post_tool_call."""
#     if block_message is not None:
#         result, error_type, error_message = json.dumps({"error": block_message}, ensure_ascii=False), block_error_type, block_message
#     else:
#         result = agent._guardrail_block_result(guardrail_decision)
#         error_type = "guardrail_block"
#         error_message = getattr(guardrail_decision, "message", None) or "Tool blocked by guardrail policy"
#     ref.emit_post(agent, result, status="blocked", error_type=error_type, error_message=error_message)
#     return result
#
#
# def _pre_tool_block(agent, ref: _ToolCallRef):
#     """Run ``pre_tool_call`` plugin hooks; returns ``(block_message, final_args)`` with any
#     hook-modified args applied. Hook failures never block."""
#     try:
#         from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
#
#         block_msg, modified_args = _dispatch_pre_tool_call_hooks(
#             ref.name,
#             ref.args,
#             **tool_hook_ids(agent, ref.task_id, ref.call_id),
#             middleware_trace=list(ref.trace),
#         )
#         return block_msg, (ref.args if modified_args is None else modified_args)
#     except Exception:
#         return None, ref.args


#  def _dispatch_authorized_once(
#      agent,
#      state: _ManagedToolResult,
#      ref: _ToolCallRef,
#      *,
#      execute,
#      scope_block: str | None,
#      display_index: int | None,
#      begin_execution,
#      authorization_gate: _ConcurrentToolAuthorizationGate | None,
#  ) -> Any:
#      """Hermes policy (scope → plugin pre-hooks → guardrails) then the one real dispatch.
#
#      Plugin ``modify`` hooks may rewrite ``ref.args`` (mirrored into ``state.args``).
#      ``begin_execution`` (concurrent start-order gate) is advanced exactly once on every
#      path so later-ordered workers keep moving; blocked calls advance it without a callback.
#      """
#      def _advance_start_order(callback=None) -> None:
#          if begin_execution is not None:
#              begin_execution(callback)
#          elif callback is not None:
#              callback()
#
#      block_message, block_error_type = scope_block, "tool_scope_block"
#      if block_message is None:
#          block_error_type = "plugin_block"
#          resolve = lambda: _pre_tool_block(agent, ref)  # noqa: E731
#          block_message, ref.args = resolve() if authorization_gate is None else authorization_gate.run(resolve)
#          state.args = ref.args
#
#      guardrail_decision = None
#      if block_message is None:
#          guardrail_decision = agent._tool_guardrails.before_call(ref.name, ref.args)
#          if guardrail_decision.allows_execution:
#              guardrail_decision = None
#
#      if block_message is not None or guardrail_decision is not None:
#          _advance_start_order()
#          state.blocked = True
#          return _blocked_tool_result(
#              agent, ref,
#              block_message=block_message, block_error_type=block_error_type, guardrail_decision=guardrail_decision,
#          )
#
#      if ref.name == "memory":
#          agent._turns_since_memory = 0
#      elif ref.name == "skill_manage":
#          agent._iters_since_skill = 0
#
#      from agent.terminal_approval_batch import prepare_current_terminal
#      prepare_current_terminal(ref)
#      _advance_start_order(lambda: _begin_tool_execution(agent, ref, display_index))
#      return _run_with_activity_heartbeat(agent, ref.name, lambda: execute(ref.args))


# Mertina v0.1 暂不保留：Relay、请求 middleware、执行 middleware 和多层授权包装。
# def _run_agent_tool_execution_middleware(
#     agent,
#     *,
#     function_name: str,
#     function_args: dict,
#     effective_task_id: str,
#     tool_call_id: str,
#     execute,
#     scope_block: str | None = None,
#     display_index: int | None = None,
#     middleware_trace: list[dict[str, Any]] | None = None,
#     begin_execution=None,
#     authorization_gate: _ConcurrentToolAuthorizationGate | None = None,
# ) -> _ManagedToolResult:
#     """Run Relay rewrites before Hermes policy and dispatch exactly once."""
#     from agent import relay_tools
#     from hermes_cli.middleware import (
#         apply_tool_request_middleware,
#         run_tool_execution_middleware,
#     )
#
#     trace = middleware_trace if middleware_trace is not None else []
#     state = _ManagedToolResult(result=None, args=function_args, middleware_trace=trace, blocked=False, dispatched=False)
#     dispatch_lock = threading.Lock()
#
#     def _authorized_dispatch(final_args: dict[str, Any]) -> Any:
#         with dispatch_lock:
#             if state.dispatched:
#                 raise RuntimeError("Hermes tool execution callback invoked more than once")
#             state.dispatched = True
#             state.blocked = False
#             state.args = final_args
#         return _dispatch_authorized_once(
#             agent,
#             state,
#             _ToolCallRef(function_name, final_args, effective_task_id, tool_call_id, trace),
#             execute=execute,
#             scope_block=scope_block,
#             display_index=display_index,
#             begin_execution=begin_execution,
#             authorization_gate=authorization_gate,
#         )
#
#     from agent.terminal_approval_batch import bind_prepared_dispatch
#     _authorized_dispatch = bind_prepared_dispatch(_authorized_dispatch)
#
#     def _hermes_pipeline(relay_args: dict[str, Any]) -> Any:
#         request_result = apply_tool_request_middleware(
#             function_name,
#             relay_args,
#             skip_relay=True,
#             **tool_hook_ids(agent, effective_task_id, tool_call_id),
#         )
#         request_args = request_result.payload if isinstance(request_result.payload, dict) else relay_args
#         trace.clear()
#         trace.extend(request_result.trace)
#         return run_tool_execution_middleware(
#             function_name,
#             request_args,
#             lambda next_args: _authorized_dispatch(next_args if isinstance(next_args, dict) else request_args),
#             original_args=function_args,
#             **tool_hook_ids(agent, effective_task_id, tool_call_id),
#         )
#
#     state.result, _relay_args = relay_tools.execute(
#         function_name,
#         function_args,
#         _hermes_pipeline,
#         session_id=str(getattr(agent, "session_id", "") or ""),
#         tool_call_id=tool_call_id or None,
#         metadata={
#             "task_id": effective_task_id or "",
#             "turn_id": getattr(agent, "_current_turn_id", "") or "",
#             "api_request_id": getattr(agent, "_current_api_request_id", "") or "",
#             "tool_call_id": tool_call_id or "",
#         },
#     )
#     return state


# Mertina v0.1 暂不保留：复杂 future 轮询、线程中断和串行 middleware 包装。
# def _poll_sequential_future(agent, future, function_name: str, deadline: float | None, started: float, authorization_gate) -> tuple[str, Any]:
#     """Wait for the worker in interrupt-poll slices, extending the deadline by human approval
#     wait; returns ``("done", result)``, ``("timeout", None)`` or ``("interrupted", None)``.
#     A disabled deadline still polls: this loop is what makes a non-cooperative tool
#     interruptible, so no deadline must not mean no interrupt checks."""
#     _last_heartbeat = 0
#     while True:
#         wait_slice = _SEQUENTIAL_INTERRUPT_POLL_SECONDS
#         if deadline is not None:
#             remaining = deadline + authorization_gate.excluded_seconds() - time.monotonic()
#             if remaining <= 0:
#                 return "timeout", None
#             wait_slice = min(wait_slice, remaining)
#         try:
#             return "done", future.result(timeout=wait_slice)
#         except concurrent.futures.TimeoutError:
#             if agent._interrupt_requested:
#                 return "interrupted", None
#             elapsed = int(time.monotonic() - started)
#             if elapsed - _last_heartbeat >= 30:
#                 _last_heartbeat = elapsed
#                 agent._touch_activity(f"sequential tool running ({elapsed}s): {function_name}")
#
#
# def _run_sequential_tool_execution_middleware(
#     agent,
#     *,
#     function_name: str,
#     function_args: dict,
#     effective_task_id: str,
#     tool_call_id: str,
#     execute,
#     scope_block: str | None = None,
#     display_index: int | None = None,
#     middleware_trace: list[dict[str, Any]] | None = None,
# ) -> _ManagedToolResult:
#     """Run one sequential call on a worker thread under the concurrent executor's deadline.
#     Interactive tools (``clarify``) own their wait via ``agent.clarify_timeout``; the
#     generic deadline would report ``tool_timeout`` while the prompt is still live. They
#     are ``_NEVER_PARALLEL_TOOLS`` and run inline below, before any deadline is armed, so
#     they need no ``_SEQUENTIAL_DEADLINE_EXEMPT_TOOLS`` entry."""
#     timeout_s = None if function_name in _SEQUENTIAL_DEADLINE_EXEMPT_TOOLS else _resolve_sequential_tool_timeout()
#     ref = _ToolCallRef(function_name, function_args, effective_task_id, tool_call_id, middleware_trace)
#     kwargs = dict(ref.middleware_kwargs(), execute=execute, scope_block=scope_block, display_index=display_index)
#     from agent.terminal_approval_batch import take_prepared_call
#     prepared = take_prepared_call(tool_call_id)
#     if prepared is not None:
#         authorization_gate = prepared.batch.authorization_gate
#         executor = prepared.batch.executor
#         worker_tid = prepared.tids
#         future = prepared.future
#     else:
#         authorization_gate = None
#     if function_name in _NEVER_PARALLEL_TOOLS:
#         return _run_agent_tool_execution_middleware(agent, **kwargs)
#
#     from tools.daemon_pool import DaemonThreadPoolExecutor
#
#     if prepared is None:
#         authorization_gate = _ConcurrentToolAuthorizationGate()
#         worker_tid: list[int] = []
#
#     def _run() -> _ManagedToolResult:
#         with _registered_tool_worker(agent) as tid:
#             worker_tid.append(tid)
#             return _run_agent_tool_execution_middleware(agent, authorization_gate=authorization_gate, **kwargs)
#
#     if ref.trace is None:
#         ref.trace = []
#     if prepared is None:
#         executor = DaemonThreadPoolExecutor(max_workers=1)
#         future = executor.submit(propagate_context_to_thread(_run))
#     deadline = time.monotonic() + timeout_s if timeout_s is not None else None
#     started = time.monotonic()
#     abandoned = False
#     try:
#         state, result = _poll_sequential_future(agent, future, function_name, deadline, started, authorization_gate)
#         if state == "done":
#             return result
#         if state == "interrupted":
#             # interrupt() already fanned out to tracked tids, but this worker may have
#             # registered after that ran; then 3s grace (mirrors the concurrent path).
#             _interrupt_worker_tids(agent, worker_tid, reason=getattr(agent, "_tool_interrupt_reason", None))
#             concurrent.futures.wait([future], timeout=3.0)
#             if future.done() and not future.cancelled():
#                 return future.result()
#             interrupt_reason = getattr(agent, "_tool_interrupt_reason", None) or "interrupt requested"
#             message = f"[Tool execution cancelled — {function_name} was abandoned: {interrupt_reason}]"
#             logger.info(
#                 "sequential tool %s abandoned due to %s (%.1fs elapsed)",
#                 function_name, interrupt_reason, time.monotonic() - started,
#             )
#             result_cls, outcome = _ToolCancelledResult, dict(
#                 duration_ms=int((time.monotonic() - started) * 1000), status="cancelled",
#                 error_type="tool_interrupted", error_message=f"Tool execution cancelled: {interrupt_reason}",
#             )
#         else:
#             assert timeout_s is not None  # only reachable when a deadline exists
#             message = f"Error executing tool '{function_name}': timed out after {timeout_s:.1f}s"
#             logger.warning("sequential tool %s timed out after %.1fs", function_name, timeout_s)
#             result_cls, outcome = _ToolTimeoutResult, dict(
#                 duration_ms=int(timeout_s * 1000), status="timeout", error_type="tool_timeout", error_message=message,
#             )
#         abandoned = True
#         if prepared is not None:
#             # A timed-out shell may still be unwinding. Never release a later
#             # prepared command into overlapping execution.
#             prepared.batch.close()
#             agent.interrupt("terminal batch tool did not complete")
#         future.cancel()
#         if state == "timeout":
#             _interrupt_worker_tids(agent, worker_tid)
#         return _abandoned_sequential_result(agent, ref, message, result_cls, **outcome)
#     finally:
#         # Never join a wedged worker (daemon pool also keeps it out of the atexit join).
#         if prepared is None:
#             executor.shutdown(wait=not abandoned, cancel_futures=abandoned)


# Mertina v0.1 暂不保留：多套 UI/Bridge callback 的容错包装。
# def _safe_callback(callback, label: str, *args, **kwargs) -> None:
#     """Invoke a UI/bridge callback if set; a failing callback is logged, never fatal."""
#     if not callback:
#         return
#     try:
#         callback(*args, **kwargs)
#     except Exception as callback_error:
#         logging.debug("%s callback error: %s", label, callback_error)


#  def _begin_tool_execution(agent, ref: _ToolCallRef, display_index: int | None) -> None:
#      """Mertina v0.1 只记录当前工具并更新时间，不做 UI、审批或文件检查点处理。"""
#      function_name = ref.name
#      agent._current_tool = function_name
#      agent._touch_activity(f"executing tool: {function_name}")


#  def _commit_tool_result(
#      agent,
#      messages: list,
#      ref: _ToolCallRef,
#      function_result,
#      *,
#      budget: BudgetConfig,
#      tool_duration: float,
#      is_error: bool,
#      blocked: bool,
#      effect_disposition,
#      observed: bool = False,
#      error_preview: Callable[[Any], Any] = lambda result: result,
#      success_log_chars: Optional[int] = None,
#      verbose_text: Callable[[Any], Any] = lambda result: result,
#  ):
#      """Observe (``observed`` results only) and log the outcome; mark the tool done; persist/
#      spill, hint, wrap and append the result; flush the session DB; project ``tool.completed``.
#
#      Blocked calls never ran, so they are neither guardrail-observed nor fed to the file-
#      mutation verifier; ``success_log_chars`` (sequential path) also logs the completion line.
#      Returns ``(persisted_result, display_result, risk_metadata)`` (``display_result`` =
#      pre-persist content for UI previews) or ``None`` when the flush failed (stop the batch).
#      """
#      function_name, function_args, tool_call_id, effective_task_id = ref.name, ref.args, ref.call_id, ref.task_id
#      if observed:
#          if not blocked:
#              function_result = agent._append_guardrail_observation(
#                  function_name, function_args, function_result, failed=is_error, tool_call_id=tool_call_id,
#              )
#          if is_error:
#              logger.warning("Tool %s returned error (%.2fs): %s", function_name, tool_duration, error_preview(function_result))
#          elif success_log_chars is not None:
#              logger.info("tool %s completed (%.2fs, %d chars)", function_name, tool_duration, success_log_chars)
#          if not blocked:
#              try:
#                  agent._record_file_mutation_result(
#                      function_name, function_args, function_result, is_error, task_id=effective_task_id,
#                  )
#              except Exception as _ver_err:
#                  logging.debug("file-mutation verifier record failed: %s", _ver_err)
#          if agent.verbose_logging:
#              logging.debug("Tool %s completed in %.2fs", function_name, tool_duration)
#              _log_result = verbose_text(function_result)
#              logging.debug("Tool result (%d chars): %s", len(_log_result), _log_result)
#
#      agent._current_tool = None
#      _status_suffix = " (error)" if is_error else ""
#      agent._touch_activity(f"tool completed: {function_name} ({tool_duration:.1f}s){_status_suffix}")
#
#      persisted_result = function_result
#      if _is_multimodal_tool_result(persisted_result):
#          persisted_result = _persist_multimodal_text_parts(
#              persisted_result, function_name, tool_call_id, get_active_env(effective_task_id), budget,
#          )
#      else:
#          persisted_result = maybe_persist_tool_result(
#              content=persisted_result,
#              tool_name=function_name,
#              tool_use_id=tool_call_id,
#              env=get_active_env(effective_task_id),
#              config=budget,
#          )
#      _record_persisted_path_for_stub(agent, tool_call_id, persisted_result)
#
#      subdir_hints = agent._subdirectory_hints.check_tool_call(function_name, function_args)
#      if subdir_hints:
#          if _is_multimodal_tool_result(persisted_result):
#              # Hint goes on the text summary part so the model still sees it; image blocks untouched.
#              _append_subdir_hint_to_multimodal(persisted_result, subdir_hints)
#          else:
#              persisted_result += subdir_hints
#
#      # Multimodal dicts become an OpenAI-style content list; text-only servers get a
#      # string-safe fallback so a rejected image result never poisons history.
#      _tool_content = agent._tool_result_content_for_active_model(function_name, persisted_result)
#      tool_message = make_tool_result_message(function_name, _tool_content, tool_call_id, effect_disposition=effect_disposition)
#      messages.append(tool_message)
#      if not _flush_session_db_after_tool_progress(agent, messages, stage=f"tool result {function_name}"):
#          return None
#
#      if not blocked:
#          # ``tool.completed`` projects AFTER the canonical append + flush so resume can
#          # reconstruct the result even if the UI bridge dies mid-projection.
#          _safe_callback(
#              agent.tool_progress_callback, "Tool progress",
#              "tool.completed", function_name, None, None, duration=tool_duration, is_error=is_error, result=function_result,
#          )
#      return persisted_result, function_result, tool_message.get("_tool_output_risk")


# Mertina v0.1 暂不保留：多模态结果中的文本分片落盘。
# def _persist_multimodal_text_parts(result: dict, tool_name: str, tool_call_id: str, env, budget: BudgetConfig) -> dict:
#     """Spill oversized TEXT parts of a multimodal envelope through the same persistence policy as
#     string results (#95429). A ``browser_exec`` call that captured a screenshot bakes its full
#     stdout into the envelope's text part, which used to bypass ``maybe_persist_tool_result``
#     entirely and ride every later request inline. Image parts are left untouched (their size is
#     governed by the vision embed budget); a fresh dict is returned so history is never mutated."""
#     parts = result.get("content") or []
#     bounded_parts, first_replacement = [], None
#     for part in parts:
#         text = part.get("text") if isinstance(part, dict) and part.get("type") == "text" else None
#         if isinstance(text, str):
#             replaced = maybe_persist_tool_result(content=text, tool_name=tool_name, tool_use_id=tool_call_id,
#                                                  env=env, config=budget)
#             if replaced != text:
#                 part = {**part, "text": replaced}
#                 first_replacement = first_replacement or replaced
#         bounded_parts.append(part)
#     if first_replacement is None:
#         return result
#     bounded = {**result, "content": bounded_parts}
#     summary = bounded.get("text_summary")
#     # The summary is a subset of the (already spilled) part text: reuse that bounded reference instead
#     # of a second persist under the same id, which would overwrite the spill file with the summary.
#     if isinstance(summary, str) and len(summary) > budget.resolve_threshold(tool_name):
#         bounded["text_summary"] = first_replacement
#     return bounded


# Mertina v0.1 暂不保留：复杂进度开关、结果预览和 CLI 完成打印。
# def _tool_progress_enabled(agent) -> bool:
#     return not agent.quiet_mode and getattr(agent, "tool_progress_mode", "all") != "off"
#
#
# def _preview(text: str, limit: int) -> str:
#     return text[:limit] + "..." if len(text) > limit else text
#
#
# def _print_tool_completed(agent, index: int, tool_duration: float, result) -> None:
#     """Non-quiet ``✅ Tool N completed`` line (full result under verbose logging)."""
#     if agent.verbose_logging:
#         print(f"  ✅ Tool {index} completed in {tool_duration:.2f}s")
#         print(agent._wrap_verbose("Result: ", result))
#     else:
#         print(f"  ✅ Tool {index} completed in {tool_duration:.2f}s - {_preview(result if isinstance(result, str) else str(result), agent.log_prefix_chars)}")


# ── Concurrent batch machinery ──────────────────────────────────────────────


@dataclass
class _ToolOutcome:
    """One finished worker slot of a concurrent batch (``ref`` holds the final name/args/trace)."""

    ref: _ToolCallRef
    result: Any
    duration: float
    is_error: bool
    blocked: bool


# Mertina v0.1 暂不保留：并发启动顺序 gate 和 worker 启动幂等包装。
# def _start_order_gate_timeout(batch_timeout: float | None) -> float:
#     """The gate bound must sit UNDER the batch deadline, else parked workers are falsely
#     reported timed out without starting. A disabled deadline keeps the stock bound."""
#     if batch_timeout is None:
#         return _START_ORDER_GATE_TIMEOUT_S
#     return min(_START_ORDER_GATE_TIMEOUT_S, batch_timeout / 2)
#
#
# class _StartOrderGate:
#     """Serialize worker dispatch by submit order (prompts appear in call order); ``abandon()``
#     releases every parked worker so none dispatches a tool the turn already gave up on."""
#
#     def __init__(self, timeout: float) -> None:
#         self._condition = threading.Condition()
#         self._next_order = 0
#         self._timeout = timeout
#         self.abandoned = threading.Event()
#
#     def abandon(self) -> None:
#         self.abandoned.set()
#         with self._condition:
#             self._condition.notify_all()
#
#     def begin_in_order(self, order: int, callback=None, *, tool_name: str = "") -> bool:
#         """Wait for ``order``, run ``callback``, advance. Returns False if abandoned."""
#         with self._condition:
#             # Bounded wait so one wedged dispatch can't starve later-ordered workers; on
#             # expiry proceed out of order (interleaved prompts beat starvation). ``>=`` (not
#             # ``==``) releases every skipped worker at once; abandoned short-circuits.
#             in_order = self._condition.wait_for(
#                 lambda: self._next_order >= order or self.abandoned.is_set(), timeout=self._timeout,
#             )
#             if self.abandoned.is_set():
#                 return False  # the turn already synthesized this result; don't advance
#             if not in_order:
#                 logger.warning(
#                     "start-order gate timed out for %s (order=%d next=%d); proceeding out of order",
#                     tool_name or "tool", order, self._next_order,
#                 )
#             try:
#                 if callback is not None:
#                     callback()
#             finally:
#                 self._next_order = max(self._next_order, order + 1)
#                 self._condition.notify_all()
#         return True
#
#
# class _WorkerStartOnce:
#     """One worker's handle on the start-order gate: advances at most once, raising
#     ``_BatchAbandoned`` (instead of dispatching late) when the batch was abandoned."""
#
#     def __init__(self, gate: _StartOrderGate, order: int, tool_name: str) -> None:
#         self._gate, self._order, self._tool_name, self._advanced = gate, order, tool_name, False
#
#     def advance(self, callback=None) -> None:
#         if self._advanced:
#             return
#         self._advanced = True
#         if not self._gate.begin_in_order(self._order, callback, tool_name=self._tool_name):
#             raise _BatchAbandoned(self._tool_name)


#  class _ConcurrentBatch:
#      """Shared state of one concurrent tool batch: per-slot results, the start-order and
#      authorization gates, and the deadline bookkeeping the wait loop needs."""
#
#      def __init__(self, agent, messages: list, effective_task_id: str, parsed_calls: list[_ParsedCall], timeout_s: float | None) -> None:
#          self.agent = agent
#          self.messages = messages
#          self.effective_task_id = effective_task_id
#          self.parsed_calls = parsed_calls
#          self.timeout_s = timeout_s
#          self.results: list[Optional[_ToolOutcome]] = [None] * len(parsed_calls)
#          for i, pc in enumerate(parsed_calls):
#              if pc.parse_error is not None:
#                  self.results[i] = _ToolOutcome(pc.ref(effective_task_id), pc.parse_error, 0.0, True, True)
#          self.gate = _StartOrderGate(_start_order_gate_timeout(timeout_s))
#          self.authorization_gate = _ConcurrentToolAuthorizationGate()
#          self.timed_out_indices: set[int] = set()
#
#      def _dispatch_worker(self, index: int, ref: _ToolCallRef, scope_block, start_gate: _WorkerStartOnce) -> Optional[_ToolOutcome]:
#          """Run one call through the middleware and synthesize its slot outcome; ``None`` when
#          abandoned at the gate (the main thread already wrote this slot; emitting would
#          double-report the tool_call_id)."""
#          agent = self.agent
#          # Approval/sudo callbacks (thread-local) and the agent turn's ContextVars are propagated by
#          # propagate_context_to_thread() at the submit site below (GHSA-qg5c-hvr5-hjgr, #13617).
#          start = time.time()
#          blocked = dispatched = False
#          try:
#              managed = _run_agent_tool_execution_middleware(
#                  agent,
#                  **ref.middleware_kwargs(),
#                  execute=lambda next_args: agent._invoke_tool(
#                      ref.name, next_args, ref.task_id, ref.call_id,
#                      messages=self.messages,
#                      pre_tool_block_checked=True,
#                      skip_tool_request_middleware=True,
#                      skip_tool_execution_middleware=True,
#                      tool_request_middleware_trace=list(ref.trace),
#                  ),
#                  scope_block=scope_block,
#                  display_index=index + 1,
#                  begin_execution=start_gate.advance,
#                  authorization_gate=self.authorization_gate,
#              )
#              result, ref.args, ref.trace = managed.result, managed.args, managed.middleware_trace
#              blocked, dispatched = managed.blocked, managed.dispatched
#          except _BatchAbandoned:
#              logger.info("tool %s abandoned at start-order gate; skipping dispatch", ref.name)
#              return None
#          except KeyboardInterrupt:
#              with contextlib.suppress(Exception):
#                  agent.interrupt("keyboard interrupt")
#              result = ref.emit_cancelled(agent, start)
#              duration = time.time() - start
#              logger.info("tool %s cancelled (%.2fs)", ref.name, duration)
#              return _ToolOutcome(ref, result, duration, True, False)
#          except Exception as tool_error:
#              result = f"Error executing tool '{ref.name}': {tool_error}"
#              logger.error("_invoke_tool raised for %s: %s", ref.name, tool_error, exc_info=True)
#          duration = time.time() - start
#          if not blocked and not dispatched:
#              ref.emit_post(agent, result, duration_ms=int(duration * 1000))
#          is_error, _ = _detect_tool_failure(ref.name, result)
#          if is_error:
#              logger.info("tool %s failed (%.2fs): %s", ref.name, duration, str(result)[:200])
#          else:
#              result_chars = len(result) if isinstance(result, str) else len(str(result))
#              logger.info(
#                  "tool %s completed (%.2fs, %d chars)", ref.name, duration, result_chars
#              )
#          return _ToolOutcome(ref, result, duration, is_error, blocked)
#
#      def run_worker(self, index: int, start_order: int) -> None:
#          """Worker function executed in a thread."""
#          agent, pc = self.agent, self.parsed_calls[index]
#          with _registered_tool_worker(agent) as _worker_tid:
#              # An interrupt may have fanned out before our registration; apply it to our tid.
#              if agent._interrupt_requested:
#                  _interrupt_worker_tids(agent, [_worker_tid], reason=getattr(agent, "_tool_interrupt_reason", None))
#              _set_worker_activity_callback(agent)
#              start_gate = _WorkerStartOnce(self.gate, start_order, pc.name)
#              try:
#                  outcome = self._dispatch_worker(index, pc.ref(self.effective_task_id), pc.scope_block, start_gate)
#                  if outcome is not None:
#                      self.results[index] = outcome
#              finally:
#                  with contextlib.suppress(_BatchAbandoned):
#                      start_gate.advance()  # keep later-ordered workers moving
#
#      def submit_all(self, executor, runnable: list[int]) -> tuple[list, dict]:
#          """Submit every runnable slot; on interpreter shutdown, synthesize error results
#          for the unsubmitted remainder instead of raising. ``propagate_context_to_thread``
#          carries turn ContextVars and thread-local approval/sudo callbacks into the worker."""
#          futures = []
#          future_to_index = {}
#          for submit_index, i in enumerate(runnable):
#              try:
#                  f = executor.submit(propagate_context_to_thread(self.run_worker), i, submit_index)
#              except RuntimeError as submit_error:
#                  if not _is_interpreter_shutdown_submit_error(submit_error):
#                      raise
#                  skipped = runnable[submit_index:]
#                  logger.warning(
#                      "interpreter shutdown while scheduling concurrent tools; skipping %d unsubmitted tool(s)", len(skipped),
#                  )
#                  for skipped_i in skipped:
#                      ref = self.parsed_calls[skipped_i].ref(self.effective_task_id)
#                      if self.results[skipped_i] is None:
#                          result = f"Error executing tool '{ref.name}': Python interpreter is shutting down; tool was not started"
#                          self.results[skipped_i] = _ToolOutcome(ref, result, 0.0, True, False)
#                  break
#              futures.append(f)
#              future_to_index[f] = i
#          return futures, future_to_index
#
#      # Mertina v0.1 暂不保留：只用于复杂心跳和日志展示的运行工具名列表。
#      # def _running_names(self, not_done, future_to_index) -> list[str]:
#      #     return [self.parsed_calls[future_to_index[f]].name for f in not_done if f in future_to_index]
#
#      def await_completion(self, futures, future_to_index, deadline: float | None) -> bool:
#          """Wait with periodic heartbeats and interrupt checks; True when the batch was
#          abandoned (deadline or interrupt) and the executor must not join its workers."""
#          agent = self.agent
#          _conc_start = time.time()
#          while True:
#              wait_timeout = 5.0
#              if deadline is not None:
#                  remaining = deadline + self.authorization_gate.excluded_seconds() - time.monotonic()
#                  if remaining <= 0:
#                      not_done = {f for f in futures if not f.done()}
#                  else:
#                      wait_timeout = min(wait_timeout, remaining)
#              if deadline is None or remaining > 0:
#                  _done, not_done = concurrent.futures.wait(futures, timeout=wait_timeout)
#              if not not_done:
#                  return False
#
#              timed_out = deadline is not None and time.monotonic() >= deadline + self.authorization_gate.excluded_seconds()
#              if timed_out:
#                  self.timed_out_indices = {future_to_index[f] for f in not_done if f in future_to_index}
#                  logger.warning(
#                      "concurrent tool batch timed out after %.1fs; %d tool(s) still running: %s",
#                      self.timeout_s,
#                      len(self.timed_out_indices),
#                      ", ".join(self._running_names(not_done, future_to_index)[:5]),
#                  )
#              elif agent._interrupt_requested:
#                  # Tools without interrupt checks (web_search, read_file) run to
#                  # completion; cancel unstarted futures so we don't block on them.
#                  agent._vprint(
#                      f"{agent.log_prefix}⚡ Interrupt: cancelling {len(not_done)} pending concurrent tool(s)",
#                      force=True,
#                  )
#              else:
#                  _conc_elapsed = int(time.time() - _conc_start)
#                  # Heartbeat every ~30s (6 × 5s poll intervals)
#                  if _conc_elapsed > 0 and _conc_elapsed % 30 < 6:
#                      _still_running = self._running_names(not_done, future_to_index)
#                      agent._touch_activity(
#                          f"concurrent tools running ({_conc_elapsed}s, "
#                          f"{len(not_done)} remaining: {', '.join(_still_running[:3])})"
#                      )
#                  continue
#              for f in not_done:
#                  f.cancel()
#              # Release gate-parked workers BEFORE interrupt fan-out so none later
#              # dispatches a tool the turn already reported as timed out / interrupted.
#              self.gate.abandon()
#              if timed_out:
#                  with agent._tool_worker_threads_lock:
#                      worker_tids = list(agent._tool_worker_threads)
#                  _interrupt_worker_tids(agent, worker_tids)
#              else:
#                  # Give running tools a moment to notice the per-thread interrupt and exit gracefully.
#                  concurrent.futures.wait(not_done, timeout=3.0)
#              return True
#
#      def run(self) -> None:
#          """Dispatch the runnable calls on a daemon pool and wait for the batch."""
#          runnable = [i for i, pc in enumerate(self.parsed_calls) if pc.parse_error is None]
#          if not runnable:
#              return
#          deadline = time.monotonic() + self.timeout_s if self.timeout_s is not None else None
#          max_workers = _max_workers_for_tool_batch([(i, None, self.parsed_calls[i].name) for i in runnable])
#          # Daemon workers: the stdlib pool's atexit join would let one wedged tool block exit.
#          from tools.daemon_pool import DaemonThreadPoolExecutor
#          executor = DaemonThreadPoolExecutor(max_workers=max_workers)
#          abandon_executor = False
#          try:
#              futures, future_to_index = self.submit_all(executor, runnable)
#              abandon_executor = self.await_completion(futures, future_to_index, deadline)
#          finally:
#              # Every abandoning exit releases gate-parked workers and leaves wedged threads
#              # detached rather than joining them; normal completion joins.
#              if abandon_executor:
#                  self.gate.abandon()
#              executor.shutdown(wait=not abandon_executor, cancel_futures=abandon_executor)


#  def _unfinished_tool_result(agent, ref: _ToolCallRef, *, timed_out: bool, timeout_s: float | None) -> tuple[str, float, Optional[str]]:
#      """为没有返回结果的并发槽位生成超时、取消或异常结果。"""
#      if timed_out:
#          suffix = f"{timeout_s:.1f}s" if timeout_s is not None else "the configured timeout"
#          function_result = f"Error executing tool '{ref.name}': timed out after {suffix}"
#          tool_duration, effect_disposition = float(timeout_s or 0.0), "unknown"
#      elif agent._interrupt_requested:
#          function_result = f"[Tool execution cancelled — {ref.name} was skipped due to user interrupt]"
#          tool_duration, effect_disposition = 0.0, None
#      else:
#          function_result = f"Error executing tool '{ref.name}': thread did not return a result"
#          tool_duration, effect_disposition = 0.0, None
#      return function_result, tool_duration, effect_disposition


#  def _append_batch_results(agent, messages: list, effective_task_id: str, batch: _ConcurrentBatch, budget: BudgetConfig) -> bool:
#      """按模型原始调用顺序提交并发结果；提交失败时停止当前批次。"""
#      for i, pc in enumerate(batch.parsed_calls):
#          r = batch.results[i]
#          # A worker may finish between the deadline snapshot and this loop;
#          # prefer its real result over a fabricated timeout.
#          if r is None:
#              ref, is_error, blocked = pc.ref(effective_task_id), True, False
#              function_result, tool_duration, effect_disposition = _unfinished_tool_result(
#                  agent, ref, timed_out=i in batch.timed_out_indices, timeout_s=batch.timeout_s,
#              )
#          else:
#              ref, function_result, tool_duration, is_error, blocked = r.ref, r.result, r.duration, r.is_error, r.blocked
#              effect_disposition = "none" if blocked else None
#          committed = _commit_tool_result(
#              agent, messages, ref, function_result,
#              budget=budget, tool_duration=tool_duration, is_error=is_error, blocked=blocked,
#              effect_disposition=effect_disposition, observed=r is not None,
#              error_preview=lambda res: _multimodal_text_summary(res)[:200],
#          )
#          if committed is None:
#              return False
#      return True


#  def execute_tool_calls_concurrent(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> None:
#      """并发执行独立工具，并按模型原始顺序写回结果。"""
#      tool_calls = assistant_message.tool_calls
#      num_tools = len(tool_calls)
#      _tool_budget = _budget_for_agent(agent)  # once per turn, not per result
#
#      if agent._interrupt_requested:
#          _append_skipped_tool_results(
#              agent, messages, tool_calls, effective_task_id,
#              content="[Tool execution cancelled — {name} was skipped due to user interrupt]",
#          )
#          return
#
#      parsed_calls = [_parse_tool_call(agent, tc) for tc in tool_calls]
#      timeout_s = _resolve_concurrent_tool_timeout()
#      batch = _ConcurrentBatch(agent, messages, effective_task_id, parsed_calls, timeout_s)
#      batch.run()
#
#      if not _append_batch_results(agent, messages, effective_task_id, batch, _tool_budget):
#          return


# ── Sequential dispatch ─────────────────────────────────────────────────────


# Mertina v0.1 暂不保留：spinner、emoji、delegate 专用显示文案。
# def _start_quiet_tool_spinner(agent, function_name: str, function_args: dict, *, gate: bool = True, label: Optional[str] = None):
#     """Start the quiet-mode kawaii spinner for one tool call, or return None; ``gate=False``
#     skips ``_should_start_quiet_spinner`` (context-engine tools always spin)."""
#     if not agent._should_emit_quiet_tool_messages() or (gate and not agent._should_start_quiet_spinner()):
#         return None
#     face = random.choice(KawaiiSpinner.get_waiting_faces())
#     if label is None:
#         display_args = _redact_tool_args_for_display(function_name, function_args) or function_args
#         label = f"{_get_tool_emoji(function_name)} {_build_tool_label(function_name, display_args) or function_name}"
#     spinner = KawaiiSpinner(f"{face} {label}", spinner_type='dots', print_fn=agent._print_fn)
#     spinner.start()
#     return spinner
#
#
# def _finish_quiet_tool_spinner(agent, spinner, function_name: str, function_args: dict, tool_duration: float, result) -> None:
#     """Stop the spinner with the cute completion line, or print it when no spinner ran."""
#     if spinner or agent._should_emit_quiet_tool_messages():
#         cute = _get_cute_tool_message_impl(function_name, function_args, tool_duration, result=result)
#         spinner.stop(cute) if spinner else agent._vprint(f"  {cute}")
#
#
# def _delegate_spinner_label(function_args: dict) -> str:
#     action = str(function_args.get("action") or "").strip().lower()
#     tasks = function_args.get("tasks")
#     if action in ("list", "steer", "stop"):
#         return f"🔀 subagent {action}"
#     if tasks and isinstance(tasks, list):
#         return f"🔀 delegating {len(tasks)} tasks · (/agents to monitor)"
#     goal_preview = (function_args.get("goal") or "")[:30]
#     return f"🔀 {goal_preview} · (/agents to monitor)" if goal_preview else "🔀 delegating · (/agents to monitor)"


# Mertina v0.1 暂不保留：把 inline/delegate/context/memory 路由统一成 registry handler。
# @dataclass
# class _SequentialDispatch:
#     """How one sequential call executes: the callable plus its spinner/error policy."""
#
#     execute: Callable[[dict], Any]
#     spinner: Any = None
#     middleware_trace_arg: Optional[list] = None  # forwarded to the middleware runner (registry closure reads it)
#     error_result: Optional[Callable[[Exception], str]] = None  # None → exceptions propagate (inline/delegate own failures)
#     error_log: str = ""
#     handles_keyboard_interrupt: bool = False
#     is_delegate: bool = False
#     finish_spinner: bool = True
#     finish_in_finally: bool = True  # inline tools print their completion line only on success


#  def _resolve_sequential_dispatch(agent, ref: _ToolCallRef, messages: list) -> _SequentialDispatch:
#      """Pick the execute callable for one sequential call and start its spinner. Precedence:
#      inline agent-level tools, delegate_task, context-engine tools, memory-provider tools,
#      then the registry."""
#      function_name, function_args, effective_task_id, tool_call_id, middleware_trace = (
#          ref.name, ref.args, ref.task_id, ref.call_id, ref.trace,
#      )
#      if function_name != "delegate_task" and function_name in INLINE_TOOL_EXECUTORS:
#          # Agent-level tools that need live AIAgent state; table shared with invoke_tool.
#          inline_executor = INLINE_TOOL_EXECUTORS[function_name]
#          inline_ctx = InlineToolContext(effective_task_id=effective_task_id, tool_call_id=tool_call_id, messages=messages)
#          return _SequentialDispatch(lambda next_args: inline_executor(agent, next_args, inline_ctx), finish_in_finally=False)
#      if function_name == "delegate_task":
#          spinner = _start_quiet_tool_spinner(agent, function_name, function_args, label=_delegate_spinner_label(function_args))
#          agent._delegate_spinner = spinner
#          return _SequentialDispatch(agent._dispatch_delegate_task, spinner=spinner, is_delegate=True)
#      if agent._context_engine_tool_names and function_name in agent._context_engine_tool_names:
#          return _SequentialDispatch(
#              execute=lambda next_args: agent.context_compressor.handle_tool_call(function_name, next_args, messages=messages),
#              spinner=_start_quiet_tool_spinner(agent, function_name, function_args, gate=False),
#              error_result=lambda e: json.dumps({"error": f"Context engine tool '{function_name}' failed: {e}"}),
#              error_log="context_engine.handle_tool_call raised for %s: %s",
#          )
#      if agent._memory_manager and agent._memory_manager.has_tool(function_name):
#          # Memory-provider tools (hindsight_retain, honcho_search, ...) are not in the registry.
#          return _SequentialDispatch(
#              execute=lambda next_args: agent._memory_manager.handle_tool_call(function_name, next_args),
#              spinner=_start_quiet_tool_spinner(agent, function_name, function_args),
#              error_result=lambda e: json.dumps({"error": f"Memory tool '{function_name}' failed: {e}"}),
#              error_log="memory_manager.handle_tool_call raised for %s: %s",
#          )
#
#      # Registry tools: post hook is owned by this executor (inner observer suppressed).
#      def _execute(next_args: dict) -> Any:
#          import model_tools
#
#          with model_tools.suppress_post_tool_call_hook():
#              return model_tools.handle_function_call(
#                  function_name,
#                  next_args,
#                  effective_task_id,
#                  tool_call_id=tool_call_id,
#                  session_id=agent.session_id or "",
#                  turn_id=getattr(agent, "_current_turn_id", "") or "",
#                  api_request_id=getattr(agent, "_current_api_request_id", "") or "",
#                  enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
#                  skip_pre_tool_call_hook=True,
#                  skip_tool_request_middleware=True,
#                  skip_tool_execution_middleware=True,
#                  tool_request_middleware_trace=list(middleware_trace),
#                  enabled_toolsets=getattr(agent, "enabled_toolsets", None),
#                  disabled_toolsets=getattr(agent, "disabled_toolsets", None),
#              )
#
#      return _SequentialDispatch(
#          execute=_execute,
#          spinner=_start_quiet_tool_spinner(agent, function_name, function_args) if agent.quiet_mode else None,
#          middleware_trace_arg=middleware_trace,
#          error_result=lambda e: f"Error executing tool '{function_name}': {e}",
#          error_log="handle_function_call raised for %s: %s",
#          handles_keyboard_interrupt=True,
#          finish_spinner=bool(agent.quiet_mode),
#      )


#  def _skip_remaining_sequential(agent, messages: list, remaining, effective_task_id: str, *, content: str) -> bool:
#      """为剩余未启动的串行工具补充停止结果，不再打印 UI 提示或写入数据库。"""
#      return _append_skipped_tool_results(
#          agent, messages, remaining, effective_task_id, content=content,
#      )


#  def _append_invalid_arguments_result(agent, messages: list, ref: _ToolCallRef, parse_error: str) -> bool:
#      """追加参数解析错误，不执行对应工具。"""
#      messages.append(make_tool_result_message(ref.name, parse_error, ref.call_id))
#      return True


#  def _run_sequential_call(
#      agent,
#      dispatch: _SequentialDispatch,
#      ref: _ToolCallRef,
#      *,
#      scope_block: Optional[str],
#      messages: list,
#      remaining_calls,
#      display_index: int,
#      tool_start_time: float,
#  ) -> tuple[_ManagedToolResult, float]:
#      """Run one sequential call with its spinner/error policy; returns ``(managed, duration)``.
#      KeyboardInterrupt (registry tools only) emits results for THIS and every remaining call
#      before re-raising so the tool-call turn keeps matching results (alternation)."""
#      _spinner_result = None
#      try:
#          managed = _run_sequential_tool_execution_middleware(
#              agent,
#              **dict(ref.middleware_kwargs(), middleware_trace=dispatch.middleware_trace_arg),
#              execute=dispatch.execute,
#              scope_block=scope_block,
#              display_index=display_index,
#          )
#          ref.args = managed.args
#          _spinner_result = managed.result
#      except KeyboardInterrupt:
#          if not dispatch.handles_keyboard_interrupt:
#              raise
#          _spinner_result = ref.emit_cancelled(agent, tool_start_time)
#          with contextlib.suppress(Exception):
#              agent.interrupt("keyboard interrupt")
#          _append_skipped_tool_results(
#              agent, messages, remaining_calls, ref.task_id,
#              content="[Tool execution cancelled — {name} was skipped due to keyboard interrupt]",
#          )
#          raise
#      except Exception as tool_error:
#          if dispatch.error_result is None:
#              raise
#          function_result = dispatch.error_result(tool_error)
#          logger.error(dispatch.error_log, ref.name, tool_error, exc_info=True)
#          managed = _ManagedToolResult(result=function_result, args=ref.args, middleware_trace=ref.trace, blocked=False, dispatched=False)
#      finally:
#          if dispatch.is_delegate:
#              agent._delegate_spinner = None
#          tool_duration = time.time() - tool_start_time
#          if dispatch.finish_spinner and dispatch.finish_in_finally:
#              _finish_quiet_tool_spinner(agent, dispatch.spinner, ref.name, ref.args, tool_duration, _spinner_result)
#      if dispatch.finish_spinner and not dispatch.finish_in_finally:
#          _finish_quiet_tool_spinner(agent, dispatch.spinner, ref.name, ref.args, tool_duration, _spinner_result)
#      return managed, tool_duration


#  def _publish_sequential_result(agent, messages: list, ref: _ToolCallRef, managed: _ManagedToolResult, *, tool_duration: float, index: int, budget: BudgetConfig) -> bool:
#      """把一个串行工具结果交给统一提交函数；不再负责 hook、风险事件或 CLI 打印。"""
#      ref.args, ref.trace, function_result = managed.args, managed.middleware_trace, managed.result
#      _execution_timed_out = isinstance(function_result, (_ToolTimeoutResult, _ToolCancelledResult))
#      # Multimodal dict results (_multimodal=True) are not sliceable as strings.
#      _result_len = len(function_result) if isinstance(function_result, str) else len(str(function_result))
#      _is_error_result, _ = _detect_tool_failure(ref.name, function_result)
#      committed = _commit_tool_result(
#          agent, messages, ref, function_result,
#          budget=budget, tool_duration=tool_duration, is_error=_is_error_result, blocked=managed.blocked,
#          effect_disposition="unknown" if _execution_timed_out else None, observed=True,
#          error_preview=lambda res: res[:200] if isinstance(res, str) and not agent.verbose_logging else res,
#          success_log_chars=_result_len,
#          verbose_text=_multimodal_text_summary,
#      )
#      if committed is None:
#          return False
#      return True


#  def execute_tool_calls_sequential(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> None:
#      """串行执行一轮工具调用，不再按 terminal 审批批次拆分。"""
#      _execute_tool_calls_sequential(
#          agent, assistant_message, messages, effective_task_id, api_call_count, finalize=finalize,
#      )


#  def _execute_tool_calls_sequential(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, *, finalize: bool = True) -> None:
#      """按顺序遍历工具调用，处理停止、参数错误和结果提交。"""
#      _tool_budget = _budget_for_agent(agent)  # once per turn, not per result
#      tool_calls = assistant_message.tool_calls
#
#      for i, tool_call in enumerate(tool_calls, 1):
#          # Check interrupt BEFORE each tool so a "stop" during the previous one skips the rest.
#          if agent._interrupt_requested:
#              if not _skip_remaining_sequential(
#                  agent, messages, tool_calls[i - 1:], effective_task_id,
#                  content="[Tool execution cancelled — {name} was skipped due to user interrupt]",
#              ):
#                  return
#              break
#
#          pc = _parse_tool_call(agent, tool_call, flatten_probe=True)
#          ref = pc.ref(effective_task_id)
#          if pc.parse_error is not None:
#              if not _append_invalid_arguments_result(agent, messages, ref, pc.parse_error):
#                  return
#              continue
#
#          tool_start_time = time.time()
#          dispatch = _resolve_sequential_dispatch(agent, ref, messages)
#          managed, tool_duration = _run_sequential_call(
#              agent, dispatch, ref,
#              scope_block=pc.scope_block,
#              messages=messages,
#              remaining_calls=tool_calls[i - 1:],
#              display_index=i,
#              tool_start_time=tool_start_time,
#          )
#          if not _publish_sequential_result(agent, messages, ref, managed, tool_duration=tool_duration, index=i, budget=_tool_budget):
#              return
#
#          if agent._interrupt_requested and i < len(tool_calls):
#              if not _skip_remaining_sequential(
#                  agent, messages, tool_calls[i:], effective_task_id,
#                  content="[Tool execution skipped — {name} was not started. User sent a new message]",
#              ):
#                  return
#              break


# Mertina v0.1 暂不保留：并发/串行混合的 segmented 调度。
# def execute_tool_calls_segmented(agent, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0, segments=None) -> None:
#     """Execute a mixed batch as ordered parallel/sequential segments (the ``(kind, calls)``
#     plan from ``_plan_tool_batch_segments``), preserving per-call result order and barrier
#     boundaries exactly as fully-sequential execution. Turn-end work (budget + /steer) runs
#     once here (segments run with ``finalize=False``); each segment executor checks the
#     interrupt flag up front, so an interrupt drains later segments with one result per call."""
#     from types import SimpleNamespace
#
#     if segments is None:
#         _active_env = get_active_env(effective_task_id)
#         _exec_cwd = Path(_active_env.cwd) if _active_env is not None and _active_env.cwd else None
#         segments = _plan_tool_batch_segments(assistant_message.tool_calls, execution_cwd=_exec_cwd)
#
#     for kind, calls in segments:
#         if getattr(agent, "_incremental_persistence_failed", False):
#             return
#         segment_message = SimpleNamespace(tool_calls=list(calls))
#         run_segment = execute_tool_calls_concurrent if kind == "parallel" else execute_tool_calls_sequential
#         run_segment(agent, segment_message, messages, effective_task_id, api_call_count, finalize=False)
#         if getattr(agent, "_incremental_persistence_failed", False):
#             return
#
#     total_tools = len(assistant_message.tool_calls)
#     if total_tools > 0:
#         _finalize_tool_batch(agent, messages, effective_task_id, total_tools, _budget_for_agent(agent))


__all__ = [
    "execute_tool_calls_concurrent",
    "execute_tool_calls_sequential",
    # Mertina v0.1 暂不导出：segmented 调度已经被块注释保留。
]


# [改动] B：兼容 provider 返回的对象和字典形式 tool call，避免重新引入 message_sanitization 依赖。
def _tool_call_field(tool_call: Any, field_name: str, default: Any = None) -> Any:
    """[改动] 从对象或字典形式的工具调用中读取字段。"""
    if isinstance(tool_call, dict):
        return tool_call.get(field_name, default)
    return getattr(tool_call, field_name, default)


# [改动] B：直接读取原始调用 ID，保证 tool 结果与 assistant call 配对。
def _tool_call_id(tool_call: Any) -> str:
    """[改动] 取得 assistant 发出的 tool_call_id。"""
    return str(
        _tool_call_field(tool_call, "id", None)
        or _tool_call_field(tool_call, "tool_call_id", "")
        or ""
    )


# [改动] B：统一取得对象/字典 tool call 的名称，停止时也使用同一取值规则。
def _tool_call_name(tool_call: Any) -> str:
    """[改动] 取得模型请求的工具名称。"""
    function = _tool_call_field(tool_call, "function", {}) or {}
    return str(_tool_call_field(function, "name", "") or "tool")


# [改动] B：B 阶段只保留结果消息构造，不再做 Hermes 落盘、风险和 UI 投影。
def _make_tool_result_message(ref: _ToolCallRef, result: Any) -> dict:
    """[改动] 构造一条和原始 tool_call_id 配对的内存结果消息。"""
    if isinstance(result, str):
        content = result
    elif isinstance(result, dict) and result.get("_multimodal") is True:
        content = result.get("content") or result.get("text_summary") or ""
    else:
        content = json.dumps(result, ensure_ascii=False, default=str)
    return {
        "role": "tool",
        "tool_call_id": ref.call_id,
        "name": ref.name,
        "content": content,
    }


# [改动] B：工具身份只保留最小结果配对字段；旧 middleware 方法已在原位置注释保留。
@dataclass
class _ToolCallRef:
    """[改动] 保存工具名称、参数、任务 ID 和结果配对 ID。"""

    name: str
    args: dict
    task_id: str
    call_id: str


# [改动] B：去掉动态 tool-search、scope 和 middleware trace，只保留解析结果。
@dataclass
class _ParsedCall:
    """[改动] 保存一个已经完成 JSON 参数解析的工具调用。"""

    tool_call: Any
    name: str
    args: dict
    call_id: str
    parse_error: Optional[str]

    def ref(self, task_id: str) -> _ToolCallRef:
        """[改动] 将解析结果转换为执行和结果提交共用的调用身份。"""
        return _ToolCallRef(self.name, self.args, task_id, self.call_id)


# [改动] B：直接解析 provider 工具名，不再调用旧名称兼容和 tool-search bridge。
def _parse_tool_call(tool_call: Any) -> _ParsedCall:
    """[改动] 解析一个工具调用；工具存在性留给 registry dispatcher 判断。"""
    function = _tool_call_field(tool_call, "function", {}) or {}
    name = str(_tool_call_field(function, "name", "") or "")
    args, parse_error = _parse_tool_arguments(_tool_call_field(function, "arguments", "{}"))
    return _ParsedCall(tool_call, name, args, _tool_call_id(tool_call), parse_error)


# [改动] B：只使用允许工具集合和 registry.dispatch 作为实际执行边界。
def _dispatch_registered_tool(agent, ref: _ToolCallRef) -> Any:
    """[改动] 校验允许工具后调用 registry，并把边界异常转换成工具错误。"""
    configured_names = getattr(agent, "valid_tool_names", None)
    if configured_names is not None and ref.name not in {str(name) for name in configured_names}:
        return json.dumps(
            {"error": f"Tool '{ref.name}' is not available in this session."},
            ensure_ascii=False,
        )
    try:
        from tools.registry import registry

        return registry.dispatch(ref.name, ref.args)
    except Exception as exc:
        logger.exception("Minimal registry dispatch failed for %s", ref.name)
        return f"Error executing tool '{ref.name}': {type(exc).__name__}: {exc}"


# [改动] B：识别 registry 返回的常见错误，不再依赖 Hermes display 检测器。
def _result_is_error(result: Any) -> bool:
    """[改动] 判断工具结果是否表示执行失败。"""
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return result.startswith("Error executing tool")
    return isinstance(payload, dict) and bool(payload.get("error"))


# [改动] B：只保留当前工具状态字段，移除 UI、审批和检查点副作用。
def _begin_tool_execution(agent, ref: _ToolCallRef, display_index: int | None = None) -> None:
    """[改动] 尽力记录当前工具，不让缺失的宿主活动接口阻断执行。"""
    del display_index
    try:
        agent._current_tool = ref.name
    except Exception:
        pass
    touch_activity = getattr(agent, "_touch_activity", None)
    if callable(touch_activity):
        try:
            touch_activity(f"executing tool: {ref.name}")
        except Exception:
            logger.debug("tool activity update failed", exc_info=True)


# [改动] B：执行完成后只清理当前工具状态，不再触发 UI callback。
def _end_tool_execution(agent, ref: _ToolCallRef, duration: float) -> None:
    """[改动] 清理当前工具状态并尽力更新时间。"""
    try:
        agent._current_tool = None
    except Exception:
        pass
    touch_activity = getattr(agent, "_touch_activity", None)
    if callable(touch_activity):
        try:
            touch_activity(f"tool completed: {ref.name} ({duration:.1f}s)")
        except Exception:
            logger.debug("tool activity update failed", exc_info=True)


# [改动] B：统一提交函数只负责把结果追加到内存消息历史。
def _commit_tool_result(
    agent,
    messages: list,
    ref: _ToolCallRef,
    function_result: Any,
    *,
    is_error: bool = False,
    **_legacy_options,
) -> bool:
    """[改动] 追加一个工具结果；旧预算、持久化和风险参数仅为兼容而忽略。"""
    del is_error, _legacy_options
    messages.append(_make_tool_result_message(ref, function_result))
    try:
        agent._current_tool = None
    except Exception:
        pass
    return True


# [改动] B：未完成调用仍需生成结果消息，避免下一轮模型收到断裂的 tool call 序列。
def _unfinished_tool_result(agent, ref: _ToolCallRef, *, timed_out: bool, timeout_s: float | None) -> Any:
    """[改动] 合成超时、停止或线程未返回时的可见结果。"""
    if timed_out:
        suffix = f"{timeout_s:.1f}s" if timeout_s is not None else "the configured timeout"
        return f"Error executing tool '{ref.name}': timed out after {suffix}"
    if getattr(agent, "_interrupt_requested", False):
        return f"[Tool execution cancelled — {ref.name} was skipped due to user interrupt]"
    return f"Error executing tool '{ref.name}': thread did not return a result"


# [改动] B：并发只保留提交、等待、停止/超时和结果槽位，不再使用旧 gate/middleware。
class _ConcurrentBatch:
    """[改动] 管理一批独立工具的并发执行和按序结果。"""

    def __init__(self, agent, effective_task_id: str, parsed_calls: list[_ParsedCall], timeout_s: float | None) -> None:
        self.agent = agent
        self.effective_task_id = effective_task_id
        self.parsed_calls = parsed_calls
        self.timeout_s = timeout_s
        self.results: list[Optional[_ToolOutcome]] = [None] * len(parsed_calls)
        self.timed_out_indices: set[int] = set()
        for index, parsed_call in enumerate(parsed_calls):
            if parsed_call.parse_error is not None:
                self.results[index] = _ToolOutcome(
                    parsed_call.ref(effective_task_id), parsed_call.parse_error, 0.0, True, True,
                )

    def _run_one(self, index: int) -> _ToolOutcome:
        """[改动] 在线程中执行一个已经通过参数解析的工具。"""
        parsed_call = self.parsed_calls[index]
        ref = parsed_call.ref(self.effective_task_id)
        if getattr(self.agent, "_interrupt_requested", False):
            result = f"[Tool execution cancelled — {ref.name} was skipped due to user interrupt]"
            return _ToolOutcome(ref, result, 0.0, False, False)

        started = time.monotonic()
        _begin_tool_execution(self.agent, ref, index + 1)
        try:
            result = _dispatch_registered_tool(self.agent, ref)
        except KeyboardInterrupt:
            result = f"[Tool execution cancelled — {ref.name} was interrupted]"
        except Exception as exc:
            result = f"Error executing tool '{ref.name}': {type(exc).__name__}: {exc}"
        finally:
            _end_tool_execution(self.agent, ref, time.monotonic() - started)
        duration = time.monotonic() - started
        return _ToolOutcome(ref, result, duration, _result_is_error(result), False)

    def run(self) -> None:
        """[改动] 提交可执行调用并响应 stop/超时，不让线程结果直接写入消息。"""
        runnable = [index for index, call in enumerate(self.parsed_calls) if call.parse_error is None]
        if not runnable:
            return
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_max_workers_for_tool_batch(runnable),
            thread_name_prefix="mertina-tool",
        )
        future_to_index = {executor.submit(self._run_one, index): index for index in runnable}
        pending = set(future_to_index)
        deadline = time.monotonic() + self.timeout_s if self.timeout_s is not None else None
        abandon_executor = False
        try:
            while pending:
                if getattr(self.agent, "_interrupt_requested", False):
                    abandon_executor = True
                    for future in pending:
                        future.cancel()
                    for future in pending:
                        index = future_to_index[future]
                        if self.results[index] is None:
                            ref = self.parsed_calls[index].ref(self.effective_task_id)
                            result = (
                                f"[Tool execution cancelled — {ref.name} may still be running]"
                                if future.running()
                                else f"[Tool execution cancelled — {ref.name} was skipped due to user interrupt]"
                            )
                            self.results[index] = _ToolOutcome(ref, result, 0.0, False, False)
                    break

                wait_timeout = 0.2
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        abandon_executor = True
                        for future in pending:
                            future.cancel()
                            index = future_to_index[future]
                            self.timed_out_indices.add(index)
                            if self.results[index] is None:
                                ref = self.parsed_calls[index].ref(self.effective_task_id)
                                result = _unfinished_tool_result(
                                    self.agent, ref, timed_out=True, timeout_s=self.timeout_s,
                                )
                                self.results[index] = _ToolOutcome(ref, result, self.timeout_s or 0.0, True, False)
                        break
                    wait_timeout = min(wait_timeout, remaining)

                done, pending = concurrent.futures.wait(
                    pending, timeout=wait_timeout,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    index = future_to_index[future]
                    try:
                        self.results[index] = future.result()
                    except Exception as exc:
                        ref = self.parsed_calls[index].ref(self.effective_task_id)
                        result = f"Error executing tool '{ref.name}': {type(exc).__name__}: {exc}"
                        self.results[index] = _ToolOutcome(ref, result, 0.0, True, False)
        finally:
            executor.shutdown(wait=not abandon_executor, cancel_futures=abandon_executor)


# [改动] B：按 assistant 原始调用顺序追加并发结果。
def _append_batch_results(agent, messages: list, effective_task_id: str, batch: _ConcurrentBatch) -> None:
    """[改动] 保证一调用一结果，并维持原始调用顺序。"""
    for index, parsed_call in enumerate(batch.parsed_calls):
        outcome = batch.results[index]
        if outcome is None:
            ref = parsed_call.ref(effective_task_id)
            result = _unfinished_tool_result(
                agent, ref, timed_out=index in batch.timed_out_indices, timeout_s=batch.timeout_s,
            )
            outcome = _ToolOutcome(ref, result, 0.0, True, False)
        _commit_tool_result(agent, messages, outcome.ref, outcome.result, is_error=outcome.is_error)


# [改动] B：保留原公开签名，移除动态预算和 segmented 收尾。
def execute_tool_calls_concurrent(
    agent,
    assistant_message,
    messages: list,
    effective_task_id: str,
    api_call_count: int = 0,
    *,
    finalize: bool = True,
) -> None:
    """[改动] 并发执行调用者确认独立的一批工具，并按顺序追加结果。"""
    del api_call_count, finalize
    tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])
    if getattr(agent, "_interrupt_requested", False):
        _append_skipped_tool_results(
            agent, messages, tool_calls, effective_task_id,
            content="[Tool execution cancelled — {name} was skipped due to user interrupt]",
        )
        return
    parsed_calls = [_parse_tool_call(tool_call) for tool_call in tool_calls]
    batch = _ConcurrentBatch(
        agent, effective_task_id, parsed_calls, _resolve_concurrent_tool_timeout(),
    )
    batch.run()
    _append_batch_results(agent, messages, effective_task_id, batch)


# [改动] B：停止和参数错误只追加消息，不触发旧 hook 或数据库写入。
def _append_skipped_tool_results(
    agent,
    messages: list,
    tool_calls,
    effective_task_id: str,
    *,
    content: str,
) -> None:
    """[改动] 为每个未启动调用追加一条配对结果。"""
    del agent
    for tool_call in tool_calls:
        ref = _ToolCallRef(_tool_call_name(tool_call), {}, effective_task_id, _tool_call_id(tool_call))
        messages.append(_make_tool_result_message(ref, content.format(name=ref.name)))


# [改动] B：参数解析失败时不执行工具，只追加错误消息。
def _append_invalid_arguments_result(agent, messages: list, ref: _ToolCallRef, parse_error: str) -> None:
    """[改动] 追加非法参数结果。"""
    del agent
    messages.append(_make_tool_result_message(ref, parse_error))


# [改动] B：串行调用直接走 registry，统一把异常转成结果。
def _run_sequential_call(agent, ref: _ToolCallRef) -> _ToolOutcome:
    """[改动] 执行一个串行工具并返回统一结果槽。"""
    if getattr(agent, "_interrupt_requested", False):
        result = f"[Tool execution cancelled — {ref.name} was skipped due to user interrupt]"
        return _ToolOutcome(ref, result, 0.0, False, False)
    started = time.monotonic()
    _begin_tool_execution(agent, ref)
    try:
        result = _dispatch_registered_tool(agent, ref)
    except KeyboardInterrupt:
        result = f"[Tool execution cancelled — {ref.name} was interrupted]"
    except Exception as exc:
        result = f"Error executing tool '{ref.name}': {type(exc).__name__}: {exc}"
    finally:
        _end_tool_execution(agent, ref, time.monotonic() - started)
    duration = time.monotonic() - started
    return _ToolOutcome(ref, result, duration, _result_is_error(result), False)


# [改动] B：串行发布只调用最小提交函数。
def _publish_sequential_result(agent, messages: list, outcome: _ToolOutcome) -> bool:
    """[改动] 追加一个串行工具的结果消息。"""
    return _commit_tool_result(agent, messages, outcome.ref, outcome.result, is_error=outcome.is_error)


# [改动] B：内部串行循环保留停止、解析、执行、结果补全四个核心阶段。
def _execute_tool_calls_sequential(
    agent,
    assistant_message,
    messages: list,
    effective_task_id: str,
    api_call_count: int = 0,
    *,
    finalize: bool = True,
) -> None:
    """[改动] 以最小状态机顺序处理一轮工具调用。"""
    del api_call_count, finalize
    tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])
    for index, tool_call in enumerate(tool_calls):
        if getattr(agent, "_interrupt_requested", False):
            _append_skipped_tool_results(
                agent, messages, tool_calls[index:], effective_task_id,
                content="[Tool execution cancelled — {name} was skipped due to user interrupt]",
            )
            return

        parsed_call = _parse_tool_call(tool_call)
        ref = parsed_call.ref(effective_task_id)
        if parsed_call.parse_error is not None:
            _append_invalid_arguments_result(agent, messages, ref, parsed_call.parse_error)
            continue

        outcome = _run_sequential_call(agent, ref)
        _publish_sequential_result(agent, messages, outcome)
        if getattr(agent, "_interrupt_requested", False) and index + 1 < len(tool_calls):
            _append_skipped_tool_results(
                agent, messages, tool_calls[index + 1:], effective_task_id,
                content="[Tool execution skipped — {name} was not started. User sent a new message]",
            )
            return


# [改动] B：公开串行入口保留原签名，去掉 terminal 审批批次和 /steer 收尾。
def execute_tool_calls_sequential(
    agent,
    assistant_message,
    messages: list,
    effective_task_id: str,
    api_call_count: int = 0,
    *,
    finalize: bool = True,
) -> None:
    """[改动] 顺序执行一轮工具调用并保持历史消息完整。"""
    _execute_tool_calls_sequential(
        agent, assistant_message, messages, effective_task_id, api_call_count, finalize=finalize,
    )
