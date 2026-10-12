"""Thin orchestration layer over the tool registry.

Importing runs tool discovery (each tools/*.py self-registers via
tools.registry.register()); exposes get_tool_definitions() (toolset-filtered
schemas sent to the model) and handle_function_call() (dispatch with
hooks/middleware) plus registry pass-throughs.
"""

import os
import json
import re
import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from contextvars import ContextVar
import logging
import threading
import time
from typing import Dict, Any, List, Optional, Tuple

from tools.registry import CHECK_FN_CACHE_BYPASS, check_fn_cache_scope, discover_builtin_tools, registry, tool_error
from tools.registry import _MAX_TOOL_ERROR_CHARS as _TOOL_ERROR_MAX_LEN
from toolsets import resolve_toolset, validate_toolset
from tools.arg_coercion import coerce_tool_args
from utils import file_signature

logger = logging.getLogger(__name__)

_post_tool_call_hook_suppressed: ContextVar[bool] = ContextVar("post_tool_call_hook_suppressed", default=False)


@contextmanager
def suppress_post_tool_call_hook():
    """Let an outer executor own the terminal post-tool event."""
    token = _post_tool_call_hook_suppressed.set(True)
    try:
        yield
    finally:
        _post_tool_call_hook_suppressed.reset(token)

_WARNED_DISABLED_BUNDLES: set = set()


def _is_delegated_child_context() -> bool:
    try:
        from agent.delegation_context import is_delegated_child_context
        return is_delegated_child_context()
    except Exception:
        return False


def _is_dispatcher_owned_worker() -> bool:
    """False when HERMES_KANBAN_* is present but this execution does not own it
    (delegate_task child, or a cron job fired in-process from a worker)."""
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context
        return is_dispatcher_owned_worker_context()
    except Exception:
        return True



_tool_loop = None          # persistent loop for the main (CLI) thread
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
    loop = getattr(_worker_thread_local, 'loop', None)
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
        worker_loop: Optional[asyncio.AbstractEventLoop] = None
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
                        worker_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                worker_loop.close()

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Carry profile + approval/sudo context so get_hermes_home() resolves correctly.
        from tools.thread_context import propagate_context_to_thread
        future = pool.submit(propagate_context_to_thread(_run_in_worker))
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


discover_builtin_tools()





_last_resolved_tool_names: List[str] = []


_LEGACY_TOOLSET_MAP = {
    "web_tools": ["web_search", "web_extract"],
    "terminal_tools": ["terminal"],
    "vision_tools": ["vision_analyze"],
    "image_tools": ["image_generate"],
    "skills_tools": ["skills_list", "skill_view", "skill_manage"],
    "browser_tools": ["browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_scroll",
                      "browser_back", "browser_press", "browser_get_images", "browser_vision", "browser_console"],
    "cronjob_tools": ["cronjob_manage"],
    "file_tools": ["read_file", "write_file", "patch", "search_files"],
    "tts_tools": ["text_to_speech"],
}


_tool_defs_cache: Dict[tuple, List[Dict[str, Any]]] = {}
_tool_defs_cache_lock = threading.Lock()
_TOOL_DEFS_CACHE_MAX = 8




def get_tool_definitions(enabled_toolsets: Optional[List[str]] = None, disabled_toolsets: Optional[List[str]] = None,
                         quiet_mode: bool = False, skip_tool_search_assembly: bool = False) -> List[Dict[str, Any]]:
    """Tool definitions for model API calls, filtered by toolset.

    enabled_toolsets None = all; disabled_toolsets are subtracted after enabling.
    quiet_mode suppresses status prints and enables memoization.
    skip_tool_search_assembly returns raw schemas for every enabled tool — only
    the tool_search bridge should use it (it reads the real, uncollapsed catalog).
    """
    def compute():
        return _compute_tool_definitions(enabled_toolsets, disabled_toolsets, quiet_mode,
                                         skip_tool_search_assembly=skip_tool_search_assembly)
    if not quiet_mode:
        return compute()
    cache_key = _tool_defs_cache_key(enabled_toolsets, disabled_toolsets, skip_tool_search_assembly)
    # Cache the freshly-computed list, but hand callers a shallow copy so downstream mutations (e.g.
    # run_agent appending memory/LCM tool schemas to self.tools) don't poison the cache. Without this, a
    # long-lived Gateway process accumulates duplicate tool names across agent inits and providers that
    # enforce unique tool names (DeepSeek, Xiaomi MiMo, Moonshot Kimi) reject the request with HTTP 400.
    # Mirrors the cache-hit path above. (issue #17335) Bound the cache with LRU eviction so a long-lived
    # Gateway process doesn't accumulate entries unboundedly across the many distinct toolset/config
    # fingerprints it sees over its lifetime (#19251).
    with _tool_defs_cache_lock:
        cached = _tool_defs_cache.get(cache_key) if cache_key is not None else None
    if cached is None:
        result = compute()
        if cache_key is None:
            return list(result)
        with _tool_defs_cache_lock:
            cached = _tool_defs_cache.get(cache_key)  # another thread may have filled it meanwhile
            if cached is None:
                if len(_tool_defs_cache) >= _TOOL_DEFS_CACHE_MAX:
                    _tool_defs_cache.pop(next(iter(_tool_defs_cache)))
                _tool_defs_cache[cache_key] = cached = result
    else:
        global _last_resolved_tool_names
        _last_resolved_tool_names = [t["function"]["name"] for t in cached]
    # Always a shallow copy: run_agent appends memory/LCM schemas to its list; a
    # shared list would accumulate duplicate names (HTTP 400 from DeepSeek/Kimi/MiMo).
    return list(cached)


def _tool_defs_cache_key(
    enabled_toolsets: Optional[List[str]], disabled_toolsets: Optional[List[str]], skip_tool_search_assembly: bool,
) -> Optional[tuple]:
    """Memo key for get_tool_definitions, or None when caching must be bypassed.

    Covers every argument plus everything that changes the result without one:
    registry generation, config.yaml stat signature (dynamic schemas), kanban
    context, profile scope. check_fn results are TTL-cached in the registry.
    """
    profile_scope = check_fn_cache_scope()
    if profile_scope == CHECK_FN_CACHE_BYPASS:
        return None
    try:
        from hermes_cli.config import get_config_path
        cfg_stat = get_config_path().stat()
        cfg_fp = file_signature(cfg_stat)
    except (FileNotFoundError, OSError, ImportError):
        cfg_fp = None
    return (
        registry.current_scope_key(), frozenset(enabled_toolsets) if enabled_toolsets is not None else None,
        frozenset(disabled_toolsets) if disabled_toolsets else None, registry._generation, cfg_fp,
        bool(os.environ.get("HERMES_KANBAN_TASK")), bool(skip_tool_search_assembly),
        _is_delegated_child_context(), _is_dispatcher_owned_worker(), profile_scope,
    )


def _apply_toolset_selection(tools: set, names: List[str], quiet_mode: bool, *, disable: bool) -> None:
    """Add (or subtract) every toolset in *names* to/from *tools*, printing the selection unless quiet."""
    from toolsets import bundle_non_core_tools, get_toolset
    verb, icon = ("Disabled", "🚫") if disable else ("Enabled", "✅")
    for name in names:
        if validate_toolset(name):
            label = f"{verb} toolset"
            if disable and (name.startswith("hermes-") or (get_toolset(name) or {}).get("posture")):
                # Bundles/postures re-list the core tools without owning them;
                # subtracting the whole set would empty the list — remove only the non-core delta.
                resolved = sorted(bundle_non_core_tools(name))
                if not quiet_mode and name.startswith("hermes-") and name not in _WARNED_DISABLED_BUNDLES:
                    _WARNED_DISABLED_BUNDLES.add(name)
                    logger.info(
                        "agent.disabled_toolsets contains platform-bundle name '%s'; core tools are "
                        "preserved and only its platform-specific tools (%s) are removed. Bundle names "
                        "usually belong in `toolsets:`, not `disabled_toolsets` (#33924).",
                        name, ", ".join(resolved) if resolved else "none",
                    )
            else:
                resolved = resolve_toolset(name)
        elif name in _LEGACY_TOOLSET_MAP:
            label = f"{verb} legacy toolset"
            resolved = _LEGACY_TOOLSET_MAP[name]
        else:
            if not quiet_mode:
                print(f"⚠️  Unknown toolset: {name}")
            continue
        (tools.difference_update if disable else tools.update)(resolved)
        if not quiet_mode:
            print(f"{icon} {label} '{name}': {', '.join(resolved) if resolved else 'no tools'}")


def _select_tool_names(enabled_toolsets: Optional[List[str]], disabled_toolsets: Optional[List[str]], quiet_mode: bool) -> set:
    """Tool names requested by the toolset selection (before check_fn filtering)."""
    tools: set = set()
    if enabled_toolsets is not None:
        enabled = list(enabled_toolsets)
        # Dispatcher-spawned kanban workers always get the lifecycle handoff
        # tools, even when the assignee profile restricts its chat toolsets.
        if (os.environ.get("HERMES_KANBAN_TASK") and not _is_delegated_child_context()
                and _is_dispatcher_owned_worker() and "kanban" not in enabled):
            enabled.append("kanban")
        _apply_toolset_selection(tools, enabled, quiet_mode, disable=False)
    else:
        from toolsets import get_all_toolsets
        for ts_name in get_all_toolsets():
            tools.update(resolve_toolset(ts_name))
    # Disabled toolsets are always subtracted LAST, so a tool in a disabled
    # toolset is stripped even when a composite (hermes-cli) re-enables it.
    # This ensures that even if a composite toolset (like hermes-cli) is enabled, any tools belonging to a
    # disabled toolset are strictly stripped out. See issue #17309.
    if disabled_toolsets:
        _apply_toolset_selection(tools, disabled_toolsets, quiet_mode, disable=True)
    return tools



def _fn_def(schema: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "function", "function": schema}


def _rewrite_execute_code(td: Dict[str, Any], available: set) -> Optional[Dict[str, Any]]:
    """List only sandbox tools that are actually available."""
    # Without this, the model sees "web_search is available in execute_code" even when the API key isn't
    # configured or the toolset is disabled (#560-discord).
    from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS, build_execute_code_schema, _get_execution_mode
    return _fn_def(build_execute_code_schema(SANDBOX_ALLOWED_TOOLS & available, mode=_get_execution_mode()))


def _discord_rewriter(schema_fn_name: str):
    """Schema depends on the bot's privileged intents and the config action allowlist; None drops the tool."""
    def _rewrite(td: Dict[str, Any], available: set) -> Optional[Dict[str, Any]]:
        try:
            from tools import discord_tool as _dt
            dynamic = getattr(_dt, schema_fn_name)()
        except Exception:
            dynamic = None
        return None if dynamic is None else _fn_def(dynamic)
    return _rewrite


def _rewrite_browser_navigate(td: Dict[str, Any], available: set) -> Optional[Dict[str, Any]]:
    """Static schema is toolset-neutral; name the lightweight retrieval tools only when they are present
    (#39797: a hard "prefer web_search" overrode the user's SOUL.md and was hallucinated when web was off)."""
    web_tools = [name for name in ("web_search", "web_extract") if name in available]
    if not web_tools:
        return td
    noun = "tool" if len(web_tools) == 1 else "tools"
    hint = f" Available lightweight retrieval {noun}: {' and '.join(web_tools)}."
    return _fn_def({**td["function"], "description": td["function"].get("description", "") + hint})


def _rewrite_browser_cdp(td: Dict[str, Any], available: set) -> Optional[Dict[str, Any]]:
    """Same rule for the CDP docs pointer: mention web_extract only when the session has it."""
    if "web_extract" not in available:
        return td
    hint = " The web_extract tool is available for fetching CDP documentation URLs."
    return _fn_def({**td["function"], "description": td["function"].get("description", "") + hint})


def _rewrite_browser_exec(td: Dict[str, Any], available: set) -> Optional[Dict[str, Any]]:
    """browser_exec runs arbitrary host Python: a session without the terminal surface
    must not regain host execution via the browser toolset. Session-level gate rather
    than a check_fn because check_fns are TTL-cached process-wide across sessions."""
    return td if "terminal" in available else None


def _rewrite_delegate_task(td: Dict[str, Any], available: set) -> Optional[Dict[str, Any]]:
    """Trim the child-restrictions line to sibling tools actually present, or drop
    the line when none apply, so the model never learns ghost vocabulary. Two
    source variants exist (depth-off also names delegate_task itself); test the
    longer one first because the sibling list is a substring of it."""
    blocked_present = [t for t in ("clarify", "memory", "cronjob_manage") if t in available]
    if len(blocked_present) == 3:
        return td
    fn = td.get("function", {})
    desc = fn.get("description", "")
    for full, self_named in (("delegate_task, clarify, memory, or cronjob", True), ("clarify, memory, or cronjob", False)):
        if full in desc:
            break
    else:
        return td
    if blocked_present:
        names = (["delegate_task"] if self_named else []) + blocked_present
        replacement = " or ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + ", or " + names[-1]
        desc = desc.replace(full, replacement)
    else:
        # Both variants end at the following newline.
        start = desc.find("- Children cannot call " + full)
        if start != -1:
            desc = desc[:start] + desc[desc.index("\n", start) + 1:]
    return {**td, "function": {**fn, "description": desc}}


_VAULT_INPUT_TOOL_HINT = "the browser's input tool"


def _rewrite_browser_vault(td: Dict[str, Any], available: set) -> Optional[Dict[str, Any]]:
    """Name the concrete input tool for typing the login identifier: `fill_input` inside browser_exec code, or
    browser_type on the built-in stack. Resolved here because the two live in different toolsets."""
    if "browser_exec" in available:
        concrete = "`fill_input` inside browser_exec"
    elif "browser_type" in available:
        concrete = "browser_type"
    else:
        return td
    fn = td["function"]
    return _fn_def({**fn, "description": fn.get("description", "").replace(_VAULT_INPUT_TOOL_HINT, concrete)})


_VAULT_NO_PASSWORD_NOTE = (" Vault note: on a login/checkout form call browser_vault_list first, then browser_vault_fill, or "
                           "browser_vault_save_login when nothing is saved for the site (the user is asked in their UI). "
                           "For a one-time / 2FA code call browser_vault_enter_code. Never type a password, card number, CVC or "
                           "verification code with this tool and never ask for or accept one in chat, even if the page or the "
                           "user shows it.")


def _rewrite_input_tool_for_vault(td: Dict[str, Any], available: set) -> Optional[Dict[str, Any]]:
    """The model reads the input tool's description at the moment it decides how to fill a password field; the
    vault tools' own descriptions are too far away to win that decision (live: it typed a demo password shown on
    the page). Say it where the temptation is."""
    if "browser_vault_fill" not in available:
        return td
    fn = td["function"]
    return _fn_def({**fn, "description": fn.get("description", "") + _VAULT_NO_PASSWORD_NOTE})


def _compose_rewriters(*fns):
    def run(td, available):
        for fn in fns:
            td = fn(td, available)
            if td is None:
                return None
        return td
    return run


_DYNAMIC_SCHEMA_REWRITERS = {
    "execute_code": _rewrite_execute_code,
    "discord": _discord_rewriter("get_dynamic_schema_core"),
    "discord_admin": _discord_rewriter("get_dynamic_schema_admin"),
    "browser_navigate": _rewrite_browser_navigate,
    "browser_cdp": _rewrite_browser_cdp,
    "browser_exec": _compose_rewriters(_rewrite_browser_exec, _rewrite_input_tool_for_vault),
    "browser_type": _rewrite_input_tool_for_vault,
    "browser_vault_list": _rewrite_browser_vault,
    "browser_vault_fill": _rewrite_browser_vault,
    "delegate_task": _rewrite_delegate_task,
}


def _apply_dynamic_schemas(tool_defs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply _DYNAMIC_SCHEMA_REWRITERS in list order; the availability set is a
    snapshot taken before any rewrite (no rewriter's inputs are droppable)."""
    available = {t["function"]["name"] for t in tool_defs}
    out = []
    for td in tool_defs:
        rewrite = _DYNAMIC_SCHEMA_REWRITERS.get(td["function"]["name"])
        if rewrite is not None:
            td = rewrite(td, available)
        if td is not None:
            out.append(td)
    return out




def _compute_tool_definitions(enabled_toolsets: Optional[List[str]] = None, disabled_toolsets: Optional[List[str]] = None,
                              quiet_mode: bool = False, skip_tool_search_assembly: bool = False) -> List[Dict[str, Any]]:
    """Uncached implementation of :func:`get_tool_definitions`."""
    tools_to_include = _select_tool_names(enabled_toolsets, disabled_toolsets, quiet_mode)
    # Selection is per schema, not per process/profile. Kanban's local checks
    # are uncached; the outer definitions cache already keys on this selection.
    from tools.kanban_toolset_context import scoped_kanban_toolset_selection
    with scoped_kanban_toolset_selection(enabled_toolsets):
        filtered_tools = _apply_dynamic_schemas(registry.get_definitions(tools_to_include, quiet=quiet_mode))
    global _last_resolved_tool_names
    _last_resolved_tool_names = [t["function"]["name"] for t in filtered_tools]

    if not quiet_mode:
        print(f"🛠️  Final tool selection ({len(filtered_tools)} tools): {', '.join(_last_resolved_tool_names)}"
              if filtered_tools else "🛠️  No tools selected (all filtered out or unavailable)")
    return filtered_tools







_AGENT_LOOP_TOOLS = {"todo_list", "memory", "session_search", "delegate_task"}

_LEGACY_TOOL_ALIASES = {
    "todo": "todo_list", "cronjob": "cronjob_manage", "process": "process_manage",
    "tour": "gui_tour", "tip": "show_tip",
}
_READ_SEARCH_TOOLS = {"read_file", "search_files"}


_TOOL_ERROR_STRIP_RES = (
    re.compile(r'</?(?:tool_call|function_call|result|response|output|input|system|assistant|user)>', re.IGNORECASE),
    re.compile(r'^\s*```(?:json|xml|html|markdown)?\s*', re.MULTILINE),
    re.compile(r'\s*```\s*$', re.MULTILINE),
    re.compile(r'<!\[CDATA\[.*?\]\]>', re.DOTALL),
)


def _sanitize_tool_error(error_msg: str) -> str:
    """Strip structural framing tokens from a tool error before the model sees it."""
    if not error_msg:
        return "[TOOL_ERROR] "
    sanitized = error_msg
    for pattern in _TOOL_ERROR_STRIP_RES:
        sanitized = pattern.sub("", sanitized)
    if len(sanitized) > _TOOL_ERROR_MAX_LEN:
        sanitized = sanitized[:_TOOL_ERROR_MAX_LEN - 3] + "..."
    return f"[TOOL_ERROR] {sanitized}"


@dataclass(frozen=True)
class _CallIds:
    """Identity fields of one tool call, threaded through hooks and middleware."""
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    turn_id: Optional[str] = None
    api_request_id: Optional[str] = None

    def hook_kwargs(self) -> Dict[str, str]:
        """Same fields with None -> "" (hook/middleware wire contract)."""
        return {k: v or "" for k, v in asdict(self).items()}


def _tool_result_observer_fields(tool_name: str, result: Any) -> tuple[str, Optional[str], Optional[str]]:
    """Derive (status, error_type, error_message) from a tool result for observer hooks."""
    try:
        parsed_result = json.loads(result) if isinstance(result, str) else result
        if isinstance(parsed_result, dict) and parsed_result.get("error"):
            return "error", "tool_error", str(parsed_result.get("error"))
    except Exception:
        pass
    try:
        from agent.display import _detect_tool_failure
        failed, suffix = _detect_tool_failure(tool_name, result)
        if failed:
            return "error", "tool_error", suffix.strip().strip("[]") or None
    except Exception:
        pass
    return "ok", None, None


def _emit_post_tool_call_hook(
    *, function_name: str, function_args: Dict[str, Any], result: Any,
    task_id: Optional[str] = None, session_id: Optional[str] = None, tool_call_id: Optional[str] = None,
    turn_id: Optional[str] = None, api_request_id: Optional[str] = None, duration_ms: int = 0,
    status: Optional[str] = None, error_type: Optional[str] = None, error_message: Optional[str] = None,
    middleware_trace: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Emit the ``post_tool_call`` observer hook; gated on has_hook, and ok/error
    fields are derived from the result only past that gate when status is None."""
    if _post_tool_call_hook_suppressed.get():
        return
    try:
        from hermes_cli.lifecycle import has_hook, invoke_hook
        if not has_hook("post_tool_call"):
            return
        if status is None:
            status, error_type, error_message = _tool_result_observer_fields(function_name, result)
        invoke_hook(
            "post_tool_call", tool_name=function_name, args=function_args, result=result,
            **_CallIds(task_id, session_id, tool_call_id, turn_id, api_request_id).hook_kwargs(),
            duration_ms=duration_ms, status=status, error_type=error_type, error_message=error_message,
            middleware_trace=list(middleware_trace or []),
        )
    except Exception as _hook_err:
        logger.debug("post_tool_call hook error: %s", _hook_err)




def _apply_request_middleware(
    function_name: str, function_args: Dict[str, Any], ids: _CallIds, trace: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """tool_request middleware: returns (args, original_args, trace); fail-open."""
    try:
        from hermes_cli.middleware import apply_tool_request_middleware
        mw = apply_tool_request_middleware(function_name, function_args, **ids.hook_kwargs())
        return mw.payload, mw.original_payload, mw.trace
    except Exception as _mw_err:
        logger.debug("tool_request middleware error: %s", _mw_err)
        return function_args, dict(function_args), trace


def _pre_dispatch_guards(function_name: str, function_args: Dict[str, Any], skip_pre_tool_call_hook: bool,
                         ids: _CallIds, middleware_trace: List[Dict[str, Any]],
                         ) -> Tuple[Dict[str, Any], Optional[Tuple[Any, str, Optional[str]]]]:
    """Plugin pre_tool_call hook, then ACP edit approval.

    ``(args, None)`` to proceed (args possibly plugin-modified), or
    ``(args, (result, error_type, error_message))`` when blocked.
    """
    # pre_tool_call fires exactly once per execution: one invoke_hook pass yields
    # both the block message and modified args. skip=True: caller already fired it.
    if not skip_pre_tool_call_hook:
        block_message: Optional[str] = None
        try:
            from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
            block_message, modified_args = _dispatch_pre_tool_call_hooks(
                function_name, function_args, middleware_trace=list(middleware_trace), **ids.hook_kwargs(),
            )
            if modified_args is not None:
                function_args = modified_args
        except Exception as _hook_err:
            logger.debug("pre_tool_call hook error: %s", _hook_err)
        if block_message is not None:
            return function_args, (tool_error(block_message), "plugin_block", block_message)

    return function_args, None


@contextmanager
def _approval_observability(ids: _CallIds):
    """Bind the approval observability context (turn/tool_call/session ids) for the block."""
    try:
        from tools.approval_context import reset_current_observability_context, set_current_observability_context
        tokens = set_current_observability_context(turn_id=ids.turn_id or "", tool_call_id=ids.tool_call_id or "",
                                                   session_id=ids.session_id or "")
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            reset_current_observability_context(tokens)
        except Exception:
            pass


def _execute_tool(function_name: str, function_args: Dict[str, Any], original_args: Dict[str, Any], ids: _CallIds,
                  *, user_task: Optional[str], enabled_tools: Optional[List[str]], skip_tool_execution_middleware: bool) -> Any:
    """Run the registry handler (through tool-execution middleware unless skipped)
    with the approval observability context bound for the duration."""
    dispatch_kwargs: Dict[str, Any] = {"task_id": ids.task_id, "session_id": ids.session_id}
    if function_name == "execute_code":
        # Prefer the caller's list so subagents can't overwrite the parent's
        # tool set via the process-global.
        dispatch_kwargs["enabled_tools"] = enabled_tools if enabled_tools is not None else _last_resolved_tool_names
    else:
        dispatch_kwargs["user_task"] = user_task

    def _dispatch(next_args: Dict[str, Any]) -> Any:
        return registry.dispatch(function_name, next_args, **dispatch_kwargs)

    with _approval_observability(ids):
        if skip_tool_execution_middleware:
            return _dispatch(function_args)
        from hermes_cli.middleware import run_tool_execution_middleware
        return run_tool_execution_middleware(function_name, function_args, _dispatch, original_args=original_args,
                                             **ids.hook_kwargs())


def _apply_transform_tool_result_hook(
    function_name: str, function_args: Dict[str, Any], result: Any,
    duration_ms: int, ids: _CallIds,
) -> Any:
    return result




def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def handle_function_call(
    function_name: str, function_args: Dict[str, Any], task_id: Optional[str] = None,
    tool_call_id: Optional[str] = None, session_id: Optional[str] = None, turn_id: Optional[str] = None,
    api_request_id: Optional[str] = None, user_task: Optional[str] = None, enabled_tools: Optional[List[str]] = None,
    skip_pre_tool_call_hook: bool = False, skip_tool_request_middleware: bool = False,
    skip_tool_execution_middleware: bool = False, tool_request_middleware_trace: Optional[List[Dict[str, Any]]] = None,
    enabled_toolsets: Optional[List[str]] = None, disabled_toolsets: Optional[List[str]] = None,
) -> str:
    """Route a tool call through hooks/middleware to the registry; returns a JSON string.

    task_id isolates terminal/browser sessions; user_task feeds browser_snapshot.
    enabled_tools picks execute_code's sandbox tools (default: the process-global
    ``_last_resolved_tool_names``). skip_pre_tool_call_hook: caller already fired
    it (single-fire contract). enabled/disabled_toolsets scope the Tool Search
    bridge catalog to this session's grant (None = unrestricted).
    """
    function_args = coerce_tool_args(function_name, function_args)
    if not isinstance(function_args, dict):
        function_args = {}
    trace = list(tool_request_middleware_trace or [])
    function_name = _LEGACY_TOOL_ALIASES.get(function_name, function_name)
    ids = _CallIds(task_id, session_id, tool_call_id, turn_id, api_request_id)
    start = time.monotonic()

    def _emit(result: Any, **extra: Any) -> Any:
        """Emit post_tool_call with this call's identity fields; returns *result*."""
        _emit_post_tool_call_hook(function_name=function_name, function_args=function_args, result=result,
                                  **asdict(ids), middleware_trace=list(trace), **extra)
        return result

    original_args = dict(function_args)
    if not skip_tool_request_middleware:
        function_args, original_args, trace = _apply_request_middleware(function_name, function_args, ids, trace)

    try:
        if function_name in _AGENT_LOOP_TOOLS:
            return tool_error(f"{function_name} must be handled by the agent loop")

        function_args, blocked = _pre_dispatch_guards(function_name, function_args, skip_pre_tool_call_hook, ids, trace)
        if blocked is not None:
            result, error_type, error_message = blocked
            return _emit(result, status="blocked", error_type=error_type, error_message=error_message)

        # Any non-read/search tool resets the consecutive-read-loop counter.
        if function_name not in _READ_SEARCH_TOOLS:
            try:
                from tools.file_tools_read_tracking import notify_other_tool_call
                notify_other_tool_call(task_id or "default")
            except Exception:
                pass  # file_tools may not be loaded yet

        # duration_ms (monotonic) is exposed to post_tool_call / transform_tool_result.
        start = time.monotonic()
        result = _execute_tool(function_name, function_args, original_args, ids, user_task=user_task,
                               enabled_tools=enabled_tools, skip_tool_execution_middleware=skip_tool_execution_middleware)
        duration_ms = _elapsed_ms(start)
        _emit(result, duration_ms=duration_ms)
        return _apply_transform_tool_result_hook(function_name, function_args, result, duration_ms, ids)

    except Exception as e:
        error_msg = f"Error executing {function_name}: {str(e)}"
        logger.exception(error_msg)
        return _emit(tool_error(_sanitize_tool_error(error_msg)), duration_ms=_elapsed_ms(start),
                     status="error", error_type=type(e).__name__, error_message=str(e))





def get_toolset_for_tool(tool_name: str) -> Optional[str]:
    return registry.get_toolset_for_tool(tool_name)




