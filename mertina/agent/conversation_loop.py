# Ported from hermes-agent agent/conversation_loop.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""The agent conversation loop — extracted from ``run_agent.AIAgent``.

``run_conversation(agent, ...)`` drives one user turn (model call, tool dispatch). Symbols
that callers patch on ``run_agent`` resolve via ``_ra`` so those patches keep working."""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from types import ModuleType
from typing import Any

from mertina.agent.message_sanitization import _sanitize_surrogates

# Phase helpers of the turn loop, bound at import so a source-tree swap cannot load a
# skewed phase mid-turn.
from mertina.agent.turn_api_call import (
    perform_api_call,
)
from mertina.agent.turn_api_request import build_api_request
from mertina.agent.turn_context import build_turn_context
from mertina.agent.turn_final_response import finish_text_response
from mertina.agent.turn_finalizer import finalize_turn
from mertina.agent.turn_iteration_prep import (
    announce_api_call,
    begin_iteration,
)
from mertina.agent.turn_loop_errors import handle_outer_loop_error
from mertina.agent.turn_request_assembly import assemble_api_request
from mertina.agent.turn_response_check import check_api_response
from mertina.agent.turn_response_intake import normalize_model_response
from mertina.agent.turn_tool_round import run_tool_round

logger = logging.getLogger(__name__)


def _ra() -> ModuleType:
    """Lazy ``run_agent`` reference so patches on ``run_agent.*`` reach this code path."""
    from mertina import run_agent

    return run_agent


def _restore_or_build_system_prompt(
    agent: Any, system_message: str | None, conversation_history: list[dict[str, Any]] | None
) -> None:
    """Build the system prompt fresh.

    Mutates ``agent._cached_system_prompt``."""
    # First turn of a new session.
    agent._cached_system_prompt = agent._build_system_prompt(system_message)


def _clone_message_for_send(msg: Any) -> Any:
    """Structural clone (dicts/lists recursively, immutable leaves shared) of a history
    message for the per-call API copy, so send-path rewrites never reach the persisted
    transcript (#80498). Cheaper than deepcopy: messages are JSON-shaped and acyclic."""
    if isinstance(msg, dict):
        return {
            k: _clone_message_for_send(v) if isinstance(v, (dict, list)) else v
            for k, v in msg.items()
        }
    if isinstance(msg, list):
        return [_clone_message_for_send(v) if isinstance(v, (dict, list)) else v for v in msg]
    return msg


@dataclass
class _LoopState:
    """Every local the turn loop threads through the phase helpers in ``agent/turn_*.py``.

    Helpers take the loop locals they need as keyword arguments named like these fields and
    return a verdict whose non-``action``/``result`` fields carry the same names;
    :func:`_run_phase` passes and copies them back by name, so a new helper input/output
    needs a field here and nothing else. Per-iteration slots are rebound by the phases
    before any later phase reads them, exactly as the former inline locals were."""

    # Fixed for the turn.
    effective_task_id: Any
    # Turn-scoped state (rebound by the phases).
    messages: Any
    active_system_prompt: Any
    api_call_count: int = 0
    final_response: Any = None
    interrupted: bool = False
    failed: bool = False
    _turn_exit_reason: str = "unknown"  # diagnostic: why the loop ended
    # Per-iteration slots.
    api_messages: Any = None
    tools_for_api: Any = None
    api_start_time: Any = None
    retry_count: int = 0
    max_retries: Any = None
    finish_reason: str = "stop"
    response: Any = None
    api_kwargs: Any = None  # None until built
    api_duration: Any = None
    assistant_message: Any = None


# _LoopState fields seeded from TurnContext (same name minus the leading underscore).
_CTX_FIELDS = frozenset(
    {
        "effective_task_id",
        "messages",
        "active_system_prompt",
    }
)
# Keyword names each phase helper takes (minus ``agent``), cached per function object.
_PHASE_PARAMS: dict[Any, tuple[str, ...]] = {}


def _run_phase(fn: Callable[..., Any], agent: Any, state: _LoopState, **extra: Any) -> Any:
    """Call phase helper ``fn`` with the loop locals it names, copy its verdict fields back.

    ``extra`` supplies non-state arguments (the caught exception). Returns the verdict so
    the caller can act on ``.action``."""
    params = _PHASE_PARAMS.get(fn)
    if params is None:
        params = _PHASE_PARAMS[fn] = tuple(
            p for p in inspect.signature(fn).parameters if p != "agent"
        )
    verdict = fn(agent, **{n: extra[n] if n in extra else getattr(state, n) for n in params})
    for f in fields(verdict):
        if f.name in ("action", "result"):
            continue
        value = getattr(verdict, f.name)
        setattr(state, f.name, value)
    return verdict


def _run_api_retry_loop(agent: Any, s: _LoopState) -> dict[str, Any] | None:
    """One API call (build → call → check).

    Returns None once the loop is left."""
    while s.retry_count < s.max_retries:
        _run_phase(build_api_request, agent, s)
        _run_phase(perform_api_call, agent, s)
        _rc = _run_phase(check_api_response, agent, s)
        if _rc.action == "break":
            return None
    return None


def _run_conversation_turn(
    agent: Any,
    user_message: Any,
    system_message: str | None = None,
    conversation_history: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Run a complete conversation with tool calling until completion; returns the result dict."""
    # Per-turn setup: build_turn_context mutates ``agent`` and returns the locals the loop reads.
    _ctx = build_turn_context(
        agent,
        user_message,
        system_message,
        conversation_history,
        task_id,
        restore_or_build_system_prompt=_restore_or_build_system_prompt,
        sanitize_surrogates=_sanitize_surrogates,
    )

    s = _LoopState(
        **{
            f.name: getattr(_ctx, f.name.lstrip("_"))
            for f in fields(_LoopState)
            if f.name in _CTX_FIELDS
        },
    )

    while s.api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0:
        if _run_phase(begin_iteration, agent, s).action == "break":
            break
        _run_phase(assemble_api_request, agent, s)
        _run_phase(announce_api_call, agent, s)

        s.api_start_time, s.retry_count, s.max_retries = time.time(), 0, agent._api_max_retries
        s.finish_reason, s.response, s.api_kwargs = "stop", None, None

        _run_api_retry_loop(agent, s)

        try:
            _run_phase(normalize_model_response, agent, s)
            _v = _run_phase(
                run_tool_round if s.assistant_message.tool_calls else finish_text_response, agent, s
            )
            if _v.action == "break":
                break
            if _v.action == "continue":
                continue
        except Exception as e:
            if _run_phase(handle_outer_loop_error, agent, s, e=e).action == "break":
                break

    # Post-loop finalization lives in agent/turn_finalizer.finalize_turn.
    result = finalize_turn(
        agent,
        **{
            name: getattr(s, name)
            for name in inspect.signature(finalize_turn).parameters
            if name != "agent"
        },
    )
    return result


def run_conversation(
    agent: Any,
    user_message: Any,
    system_message: str | None = None,
    conversation_history: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Run one turn (see ``_run_conversation_turn``)."""
    result = _run_conversation_turn(
        agent,
        user_message,
        system_message=system_message,
        conversation_history=conversation_history,
        task_id=task_id,
    )
    return result


__all__ = ["run_conversation"]
