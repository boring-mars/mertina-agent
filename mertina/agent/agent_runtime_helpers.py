# Ported from hermes-agent agent/agent_runtime_helpers.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Assorted AIAgent runtime helpers (message repair/sanitization, credential recovery, primary
runtime restore, prompt-cache policy, client construction, model switching, tool invocation).
Each function takes the parent ``AIAgent`` as ``agent`` except the stateless message helpers.
``_ra()`` resolves ``run_agent`` lazily so tests patching ``run_agent.X`` keep intercepting.
"""

from __future__ import annotations

import json
import logging
import time
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)


def _ra() -> ModuleType:
    """Lazy ``run_agent`` reference for test-patch routing."""
    from mertina import run_agent

    return run_agent


def create_openai_client(
    agent: Any, client_kwargs: dict[str, Any], *, reason: str, shared: bool
) -> Any:
    # Treat client_kwargs as read-only: callers pass agent._client_kwargs, and in-place mutation
    # leaks into later requests (a torn-down httpx transport got reused).
    # Callers pass agent._client_kwargs (or shallow copies of it) in; any in-place mutation leaks
    # back into the stored dict and is reused on subsequent requests. #10933 hit this by injecting
    # an httpx.Client transport that was torn down after the first request, so the next request
    # wrapped a closed transport and raised "Cannot send a request, as the client has been closed"
    # on every retry. The revert resolved that specific path; this copy locks the contract so future
    # transport/keepalive work can't reintroduce the same class of bug.
    client_kwargs = dict(client_kwargs)
    # Retries belong to the outer conversation loop (honors Retry-After); SDK retries would
    # double-retry inside it. auxiliary_client keeps SDK retries as it isn't wrapped.
    # Delegate all rate-limit / 5xx retry to hermes's outer conversation loop, which honors
    # Retry-After and applies adaptive/jittered backoff. The OpenAI SDK default (max_retries=2) uses
    # its own 1-2s backoff that ignores Retry-After and double-retries inside our loop — the same
    # deadlock the Anthropic clients hit (#26293). This is the single chokepoint every primary
    # OpenAI/aggregator client passes through (init, switch_model, recovery, restore,
    # request-scoped); auxiliary_client builds its own clients and keeps SDK retries because it is
    # NOT wrapped by the conversation loop.
    client_kwargs.setdefault("max_retries", 0)
    # ``process_bootstrap.OpenAI`` is a lazy SDK proxy; resolved at call time so tests can patch it.
    from mertina.agent import process_bootstrap

    client = process_bootstrap.OpenAI(**client_kwargs)
    _ra().logger.info(
        "OpenAI client created (%s, shared=%s) %s", reason, shared, agent._client_log_context()
    )
    return client


def _pre_tool_block_message(
    agent, function_name, function_args, effective_task_id, tool_call_id, middleware_trace
):
    """Plugin pre-tool-call hook verdict: ``(block_message, function_args)``; failures never block."""
    try:
        from mertina.cli.plugins import _dispatch_pre_tool_call_hooks

        block_message, modified_args = _dispatch_pre_tool_call_hooks(
            function_name,
            function_args,
            task_id=effective_task_id or "",
            session_id=getattr(agent, "session_id", "") or "",
            tool_call_id=tool_call_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
            middleware_trace=list(middleware_trace),
        )
        return block_message, (modified_args if modified_args is not None else function_args)
    except Exception:
        return None, function_args


def invoke_tool(
    agent,
    function_name: str,
    function_args: dict,
    effective_task_id: str,
    tool_call_id: str | None = None,
    messages: list = None,
    pre_tool_block_checked: bool = False,
    skip_tool_request_middleware: bool = False,
    tool_request_middleware_trace: list[dict[str, Any]] | None = None,
    skip_tool_execution_middleware: bool = False,
) -> str:
    """Invoke a single tool (agent-level or registry-dispatched) and return the result string;
    no display logic. Used by the concurrent path; the sequential path keeps its own inline
    invocation for display."""
    from mertina.agent.inline_tool_executors import (
        InlineToolContext,
        emit_terminal_post_tool_call,
        resolve_invoke_tool_executor,
        tool_hook_ids,
    )

    if not isinstance(function_args, dict):
        function_args = {}
    hook_ids = tool_hook_ids(agent, effective_task_id, tool_call_id)
    _tool_middleware_trace = list(tool_request_middleware_trace or [])
    try:
        from mertina.cli.middleware import apply_tool_request_middleware

        if not skip_tool_request_middleware:
            _tool_request_mw = apply_tool_request_middleware(
                function_name, function_args, **hook_ids
            )
            function_args = _tool_request_mw.payload
            _tool_middleware_trace = _tool_request_mw.trace
    except Exception as _mw_err:
        logger.debug("tool_request middleware error: %s", _mw_err)
    block_message: str | None = None
    if not pre_tool_block_checked:
        block_message, function_args = _pre_tool_block_message(
            agent,
            function_name,
            function_args,
            effective_task_id,
            tool_call_id,
            _tool_middleware_trace,
        )
    if block_message is not None:
        result = json.dumps({"error": block_message}, ensure_ascii=False)
        emit_terminal_post_tool_call(
            agent,
            function_name=function_name,
            function_args=function_args,
            result=result,
            effective_task_id=effective_task_id,
            tool_call_id=tool_call_id,
            status="blocked",
            error_type="plugin_block",
            error_message=block_message,
            middleware_trace=_tool_middleware_trace,
        )
        return result
    tool_start_time = time.monotonic()
    inline_executor = resolve_invoke_tool_executor(agent, function_name)
    if inline_executor is not None:
        inline_ctx = InlineToolContext(
            effective_task_id=effective_task_id, tool_call_id=tool_call_id, messages=messages
        )

        def _execute(next_args: dict) -> Any:
            result = inline_executor(agent, next_args, inline_ctx)
            emit_terminal_post_tool_call(
                agent,
                function_name=function_name,
                function_args=next_args if isinstance(next_args, dict) else function_args,
                result=result,
                effective_task_id=effective_task_id,
                tool_call_id=tool_call_id,
                duration_ms=int((time.monotonic() - tool_start_time) * 1000),
                middleware_trace=_tool_middleware_trace,
            )
            return result
    else:

        def _execute(next_args: dict) -> Any:
            dispatch_kwargs = dict(
                tool_call_id=tool_call_id,
                session_id=agent.session_id or "",
                turn_id=getattr(agent, "_current_turn_id", "") or "",
                api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
                skip_pre_tool_call_hook=True,
                skip_tool_request_middleware=True,
                enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                tool_request_middleware_trace=list(_tool_middleware_trace),
            )
            if skip_tool_execution_middleware:
                dispatch_kwargs["skip_tool_execution_middleware"] = True
            from mertina import model_tools

            return model_tools.handle_function_call(
                function_name, next_args, effective_task_id, **dispatch_kwargs
            )

    if skip_tool_execution_middleware:
        return _execute(function_args)
    from mertina.cli.middleware import run_tool_execution_middleware

    return run_tool_execution_middleware(
        function_name,
        function_args,
        lambda next_args: _execute(next_args if isinstance(next_args, dict) else function_args),
        original_args=function_args,
        **hook_ids,
    )
