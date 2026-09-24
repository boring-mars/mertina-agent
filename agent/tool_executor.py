# 版本 V0.1 变更说明
# 当前有效代码共 363 行（不含空行、注释和文档字符串），用于参数校验、串行/并发执行和结果配对。
# 注释代码用于保留原始实现、记录功能边界；下面按“行号范围 + 功能”标出具体位置。
#
# 已注释的主要功能（范围对应当前文件中的注释代码）：
#
# - [104–155、232–286、1071–1099] 工具结果落盘、文件检查点、结果预算、会话数据库增量持久化和多模态结果文本落盘；
# - [167–196、513–586、1133–1460] 人工审批 gate、并发授权、启动顺序 gate 及旧版并发批次控制；
# - [296–512] 旧入口兼容、解释器关闭时的线程池处理、动态 tool-search/scope 和旧工具名规范化；
# - [587–1069] worker 注册/心跳、插件前置 hook、guardrail、Relay/middleware、future 轮询和 UI/Bridge 回调包装；
# - [1100–1120、1461–1493] 工具进度、CLI 输出、spinner/emoji 等展示功能；
# - [1494–1746] inline/delegate/context/memory 路由、旧式串行流程和 segmented 调度。
#
# 源代码改动点：
# - [199–230、289–294] 参数 JSON 校验、固定并发批次超时和线程数上限；
# - [1749–1832] 读取对象或字典形式的调用字段，解析调用名、参数和原始调用 ID；
# - [1837–1930] 经 registry.dispatch 执行工具、维护当前工具状态，并提交配对结果消息；
# - [1935–2084] 使用 ThreadPoolExecutor 并发执行、处理中断/超时并按原始顺序提交；
# - [2088–2191] 保留串行执行入口，解析参数、分发调用，并在中断后补齐剩余调用结果。
#
# 新增代码：
# - 没有新增可执行代码；本次仅将文件头整理为与其他 V0.1 文件一致的变更说明。
#
# 当前保留函数和功能：
# - [157、160、199–230、289–294] `_MAX_TOOL_WORKERS`、`_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S`、参数解析和并发上限计算；
# - [1123–1131、1749–1930] `_ToolOutcome`、调用字段读取、registry 分发、执行状态和结果消息构造；
# - [1935–2084] `_ConcurrentBatch` 与 `execute_tool_calls_concurrent`：并发执行并依调用顺序补齐结果；
# - [2088–2191] `_execute_tool_calls_sequential` 与 `execute_tool_calls_sequential`：串行执行并维护调用结果配对。
#

"""执行一轮模型工具调用：解析参数、串行或并发分发、补齐并追加配对结果。

溯源基线是本仓库提交 59221bf 的 agent/tool_executor.py，并非已核实的上游
Hermes 提交。当前只有串行和并发入口生效；下方保留的旧实现注释不参与执行。
旧版导入的外部 helper 若不在本仓库，只能据旧调用点记录职责，不能推断其内部实现。
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
# 溯源：59221bf 中同名变量是 agent.message_sanitization.coalesce_tool_call_id 的别名。
# 当前仅读取对象形式的原始 ID，且活跃路径改用 _tool_call_id；恢复时须统一结果配对策略。
def _pairing_tool_call_id(tool_call: Any) -> str:
    """读取对象形式工具调用的原始 ID；当前执行路径不调用本函数。"""
    return str(getattr(tool_call, "id", None) or getattr(tool_call, "tool_call_id", "") or "")


# 溯源：59221bf 中的同名函数；当前跳过调用的路径改用支持字典的 _tool_call_name。
def _tc_name(tool_call: Any) -> str:
    """读取对象形式的工具名，缺失时返回 tool；当前执行路径不调用本函数。"""
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


# 溯源：沿用 59221bf 中的同名函数；非法参数仍不得进入工具分发。
def _parse_tool_arguments(raw_arguments: Any) -> tuple[dict, Optional[str]]:
    """只接受 JSON 对象参数；解析失败或值非对象时返回工具错误文本。"""
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


# 溯源：59221bf 中的同名函数通过 agent.deadline.resolve_timeout 读取动态配置。
# 当前固定返回默认批次超时；恢复配置时应保持调用方使用的超时单位为秒。
def _resolve_concurrent_tool_timeout() -> float | None:
    """返回当前并发批次使用的固定超时秒数。"""
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


# 溯源：59221bf 中的同名函数还会应用 _image_generate_parallel_limit。
# 当前只限制总线程数；若恢复图片工具，需重新核对其独立的并发上限。
def _max_workers_for_tool_batch(runnable_calls) -> int:
    """返回不超过可执行调用数和 _MAX_TOOL_WORKERS 的线程数。"""
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


# 溯源：沿用 59221bf 中的 _ToolOutcome 字段；当前提交结果主要使用 ref 和 result。
@dataclass
class _ToolOutcome:
    """保存调用结果；当前提交忽略 duration、blocked，is_error 传入后也被忽略。"""

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


# 溯源：59221bf 中的 _tc_name 和 _parse_tool_call 直接访问对象属性。
# 本函数新增字典形式输入的兼容读取；它不负责旧版的调用 ID 规范化。
def _tool_call_field(tool_call: Any, field_name: str, default: Any = None) -> Any:
    """从对象属性或字典键读取一个工具调用字段。"""
    if isinstance(tool_call, dict):
        return tool_call.get(field_name, default)
    return getattr(tool_call, field_name, default)


# 溯源：59221bf 的 _pairing_tool_call_id 委托 coalesce_tool_call_id。
# 当前直接读取原始字段，可能得到空字符串；恢复时须与 assistant 消息的 ID 策略一致。
def _tool_call_id(tool_call: Any) -> str:
    """读取对象或字典形式调用的原始配对 ID。"""
    return str(
        _tool_call_field(tool_call, "id", None)
        or _tool_call_field(tool_call, "tool_call_id", "")
        or ""
    )


# 溯源：59221bf 的 _tc_name 只读取对象属性。
# 当前增加字典形式支持，供未执行调用的结果补全使用；旧函数仍在文件中但不被调用。
def _tool_call_name(tool_call: Any) -> str:
    """读取模型请求的工具名，缺失时以 tool 填充结果消息。"""
    function = _tool_call_field(tool_call, "function", {}) or {}
    return str(_tool_call_field(function, "name", "") or "tool")


# 溯源：59221bf 从 agent.tool_dispatch_helpers 导入 make_tool_result_message，
# 并在 _commit_tool_result 中先处理结果落盘和面向模型的内容转换。
# 当前只构造内存消息；旧 helper 的内部实现不在本仓库，恢复时需核对消息字段契约。
def _make_tool_result_message(ref: _ToolCallRef, result: Any) -> dict:
    """把工具返回值转为与 ref.call_id 配对的内存 tool 消息。"""
    if isinstance(result, str):
        content = result
    elif isinstance(result, dict) and result.get("_multimodal") is True:
        content = result.get("content") or result.get("text_summary") or ""
    else:
        content = json.dumps(result, ensure_ascii=False, default=str)
    return {
        "role": "tool",
        "tool_call_id": ref.call_id,
        # [改动][溯源] ROADMAP.md:70-71；本地 openai.types.chat.ChatCompletionToolMessageParam
        # 只定义 role/content/tool_call_id，旧扩展字段保留为注释。
        # "name": ref.name,
        "content": content,
    }


# 溯源：59221bf 的同名类还携带 middleware_trace 和终态 hook 方法。
# 当前只保留执行与结果配对需要的身份；task_id 尚未传入实际 registry 分发。
@dataclass
class _ToolCallRef:
    """保存一次调用的工具名、参数、任务 ID 与结果配对 ID。"""

    name: str
    args: dict
    task_id: str
    call_id: str


# 溯源：59221bf 的同名类还保存 middleware_trace 与 scope_block。
# 当前只保存解析后的字段；不能据此认为旧版的 tool-search 范围检查仍然生效。
@dataclass
class _ParsedCall:
    """保存 provider 调用字段及 JSON 参数解析结果。"""

    tool_call: Any
    name: str
    args: dict
    call_id: str
    parse_error: Optional[str]

    # 溯源：59221bf 的同名方法在此处调用 _pairing_tool_call_id，并附带 middleware_trace。
    def ref(self, task_id: str) -> _ToolCallRef:
        """生成执行与提交共用的身份；当前 ID 来自 _tool_call_id 的原始读取。"""
        return _ToolCallRef(self.name, self.args, task_id, self.call_id)


# 溯源：59221bf 的同名函数先规范化旧工具名，再展开 tool-search bridge 并检查 scope。
# 当前只取 provider 原始名称和 JSON 参数；恢复旧路由时应先完成范围校验。
def _parse_tool_call(tool_call: Any) -> _ParsedCall:
    """解析工具名称、参数和配对 ID；非法 JSON 留给调用方生成错误结果。"""
    function = _tool_call_field(tool_call, "function", {}) or {}
    name = str(_tool_call_field(function, "name", "") or "")
    args, parse_error = _parse_tool_arguments(_tool_call_field(function, "arguments", "{}"))
    return _ParsedCall(tool_call, name, args, _tool_call_id(tool_call), parse_error)


# 溯源：59221bf 的 _dispatch_authorized_once 经 scope、插件前置 hook、guardrail
# 与审批检查后，最终通过 agent._invoke_tool 执行。当前直接走 registry.dispatch。
# valid_tool_names 为 None 时此处不会拦截；恢复旧策略时必须维持每次调用只分发一次。
def _dispatch_registered_tool(agent, ref: _ToolCallRef) -> Any:
    """按当前可选允许名单分发工具，并把分发异常转成工具结果。"""
    configured_names = getattr(agent, "valid_tool_names", None)
    if configured_names is not None and ref.name not in {str(name) for name in configured_names}:
        return json.dumps(
            {"error": f"Tool '{ref.name}' is not available in this session."},
            ensure_ascii=False,
        )
    try:
        from tools.registry import registry

        # [改动][溯源] tools/web_tools.py:web_search 需要当前 Agent 的 provider；
        # ROADMAP.md:82 要求搜索实现可替换，注册表仍保持进程内单例。
        # return registry.dispatch(ref.name, ref.args)
        return registry.dispatch(ref.name, ref.args, agent=agent)
    except Exception as exc:
        logger.exception("Minimal registry dispatch failed for %s", ref.name)
        return f"Error executing tool '{ref.name}': {type(exc).__name__}: {exc}"


# 溯源：59221bf 从 agent.display 导入 _detect_tool_failure，并使用其错误判定。
# 当前只识别 JSON error 字段或固定文本前缀；判定值暂被 _commit_tool_result 忽略。
def _result_is_error(result: Any) -> bool:
    """以当前简化规则判断字符串结果是否表示工具失败。"""
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return result.startswith("Error executing tool")
    return isinstance(payload, dict) and bool(payload.get("error"))


# 溯源：59221bf 的同名函数还发送 tool.started、tool_start_callback，
# 并在文件修改或危险 terminal 调用前建立检查点。当前只维护活动状态。
def _emit_tool_progress(agent, event: str, ref: _ToolCallRef, *, is_error: bool = False) -> None:
    """[改动][溯源] ROADMAP.md:72：按单个调用发送开始或完成事件；回调错误不影响结果配对。"""
    callback = getattr(agent, "tool_progress_callback", None)
    if not callable(callback):
        return
    try:
        callback({"event": event, "tool_name": ref.name, "tool_call_id": ref.call_id,
                  "is_error": is_error})
    except Exception:
        logger.exception("tool progress callback failed for %s", ref.name)


def _begin_tool_execution(agent, ref: _ToolCallRef, display_index: int | None = None) -> None:
    """尽力记录当前执行工具及活动时间；状态接口异常不阻断分发。"""
    del display_index
    _emit_tool_progress(agent, "tool.started", ref)
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


# 溯源：59221bf 的 _commit_tool_result 在结果提交阶段清理当前工具并更新时间。
# 当前在工具返回的 finally 中先清理，早于结果消息追加；完成回调不在此处触发。
def _end_tool_execution(agent, ref: _ToolCallRef, duration: float) -> None:
    """工具返回后清理当前工具并记录耗时，不发送结果完成事件。"""
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


# 溯源：59221bf 的同名函数还负责 guardrail、结果预算与落盘、SQLite flush
# 及 tool.completed 投影。当前只追加消息，is_error 和旧参数均不改变提交行为。
def _commit_tool_result(
    agent,
    messages: list,
    ref: _ToolCallRef,
    function_result: Any,
    *,
    is_error: bool = False,
    **_legacy_options,
) -> bool:
    """追加一个配对工具结果；返回值只表示内存追加完成。"""
    # [改动][溯源] ROADMAP.md:72 要求工具完成事件；旧版在此丢弃 is_error。
    # del is_error, _legacy_options
    del _legacy_options
    messages.append(_make_tool_result_message(ref, function_result))
    _emit_tool_progress(agent, "tool.completed", ref, is_error=is_error)
    try:
        agent._current_tool = None
    except Exception:
        pass
    return True


# 溯源：59221bf 的同名函数还发出终态 post_tool_call hook 并返回耗时等元数据。
# 当前只返回可见文本；保留每个 assistant tool call 都有配对结果的历史约束。
def _unfinished_tool_result(agent, ref: _ToolCallRef, *, timed_out: bool, timeout_s: float | None) -> Any:
    """为超时、停止或线程缺失的调用合成一条结果文本。"""
    if timed_out:
        suffix = f"{timeout_s:.1f}s" if timeout_s is not None else "the configured timeout"
        return f"Error executing tool '{ref.name}': timed out after {suffix}"
    if getattr(agent, "_interrupt_requested", False):
        return f"[Tool execution cancelled — {ref.name} was skipped due to user interrupt]"
    return f"Error executing tool '{ref.name}': thread did not return a result"


# 溯源：59221bf 的同名类还管理启动顺序、授权 gate 与 worker 线程登记。
# 当前只管理结果槽位及批次超时；停止或超时后的结果不代表已运行线程被强制终止。
class _ConcurrentBatch:
    """管理一批工具的并发执行，并为每个原始调用保留一个结果槽位。"""

    # 溯源：59221bf 的同名构造方法还初始化 start-order 和 authorization gate。
    def __init__(self, agent, effective_task_id: str, parsed_calls: list[_ParsedCall], timeout_s: float | None) -> None:
        """预留结果槽，并将参数错误直接填入对应槽位。"""
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

    # 溯源：合并了 59221bf 的 _ConcurrentBatch._dispatch_worker 与 run_worker 的主要职责。
    # 当前不登记 worker 线程，也不传播旧版的 ContextVars、middleware 或启动顺序 gate。
    def _run_one(self, index: int) -> _ToolOutcome:
        """在一个 worker 中执行已通过参数解析的调用，并返回结果槽。"""
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

    # 溯源：59221bf 的 run 经 submit_all、await_completion 和 daemon pool 管理批次。
    # 当前使用标准线程池；取消 future 只能取消未开始的调用，运行中的 handler 可能继续执行。
    def run(self) -> None:
        """提交可执行调用并轮询停止与超时；剩余空槽由提交阶段补齐。"""
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


# 溯源：59221bf 的同名函数在按序提交时还处理预算、持久化和完成事件。
# 当前只保证原始顺序与一调用一结果；放弃批次后晚到的真实结果不会覆盖占位结果。
def _append_batch_results(agent, messages: list, effective_task_id: str, batch: _ConcurrentBatch) -> None:
    """按 assistant 原始调用顺序向内存历史追加整批结果。"""
    for index, parsed_call in enumerate(batch.parsed_calls):
        outcome = batch.results[index]
        if outcome is None:
            ref = parsed_call.ref(effective_task_id)
            result = _unfinished_tool_result(
                agent, ref, timed_out=index in batch.timed_out_indices, timeout_s=batch.timeout_s,
            )
            outcome = _ToolOutcome(ref, result, 0.0, True, False)
        _commit_tool_result(agent, messages, outcome.ref, outcome.result, is_error=outcome.is_error)


# 溯源：59221bf 的同名公开入口还计算结果预算并参与 segmented 收尾。
# 当前保留 api_call_count 与 finalize 形参但忽略两者；调用方须先判断整批可并发。
def execute_tool_calls_concurrent(
    agent,
    assistant_message,
    messages: list,
    effective_task_id: str,
    api_call_count: int = 0,
    *,
    finalize: bool = True,
) -> None:
    """并发执行调用者已确认可并发的一批工具，并按原调用顺序追加结果。"""
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


# 溯源：59221bf 的同名函数还可发取消 hook 并逐条 flush 会话数据库。
# 当前只补齐未启动调用的内存消息，避免下一轮模型收到不完整的调用序列。
def _append_skipped_tool_results(
    agent,
    messages: list,
    tool_calls,
    effective_task_id: str,
    *,
    content: str,
) -> None:
    """为每个未启动的 assistant 工具调用追加一条配对结果。"""
    # [改动][溯源] ROADMAP.md:72,77-79：被跳过的调用也发送完成事件。
    # del agent
    for tool_call in tool_calls:
        ref = _ToolCallRef(_tool_call_name(tool_call), {}, effective_task_id, _tool_call_id(tool_call))
        messages.append(_make_tool_result_message(ref, content.format(name=ref.name)))
        _emit_tool_progress(agent, "tool.completed", ref, is_error=True)


# 溯源：59221bf 的同名函数还发出 invalid_tool_arguments 终态 hook，
# 并经旧 _commit_tool_result 完成持久化；当前只追加内存错误消息。
def _append_invalid_arguments_result(agent, messages: list, ref: _ToolCallRef, parse_error: str) -> None:
    """不执行参数非法的调用，直接追加其配对错误结果。"""
    # [改动][溯源] ROADMAP.md:72：无效参数的调用也发送完成事件。
    # del agent
    messages.append(_make_tool_result_message(ref, parse_error))
    _emit_tool_progress(agent, "tool.completed", ref, is_error=True)


# 溯源：59221bf 的同名函数经 _run_sequential_tool_execution_middleware
# 在线程中轮询停止及单工具超时。当前在调用线程直接分发，运行期间不再轮询。
def _run_sequential_call(agent, ref: _ToolCallRef) -> _ToolOutcome:
    """同步执行一个工具，将异常或正常返回值封装为结果槽。"""
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


# 溯源：59221bf 的同名函数还投影完成事件并展示结果，
# 旧 _commit_tool_result 负责预算与数据库 flush；当前只追加内存消息。
def _publish_sequential_result(agent, messages: list, outcome: _ToolOutcome) -> bool:
    """把一个串行结果交给当前最小提交函数。"""
    return _commit_tool_result(agent, messages, outcome.ref, outcome.result, is_error=outcome.is_error)


# 溯源：59221bf 的同名循环还检查增量持久化失败，并在结束时执行预算和 steer 收尾。
# 当前只在调用前后检查停止，并补齐剩余调用的结果；finalize 形参被忽略。
def _execute_tool_calls_sequential(
    agent,
    assistant_message,
    messages: list,
    effective_task_id: str,
    api_call_count: int = 0,
    *,
    finalize: bool = True,
) -> None:
    """顺序解析和执行一轮工具调用，为每个调用追加结果或跳过说明。"""
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


# 溯源：59221bf 的同名公开入口通过 terminal_approval_batch 分组，
# 并在 finalize 时统一执行预算与 steer 收尾；当前仅转发到内部串行循环。
def execute_tool_calls_sequential(
    agent,
    assistant_message,
    messages: list,
    effective_task_id: str,
    api_call_count: int = 0,
    *,
    finalize: bool = True,
) -> None:
    """顺序执行一轮工具调用；保留旧签名以供 run_agent 调用。"""
    _execute_tool_calls_sequential(
        agent, assistant_message, messages, effective_task_id, api_call_count, finalize=finalize,
    )
