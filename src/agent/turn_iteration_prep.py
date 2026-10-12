"""Outer-iteration bookkeeping for the conversation turn loop, in call order:
``begin_iteration`` (pending redirect, interrupt / review-budget / iteration-budget exits),
``prepare_iteration`` (``agent:step`` callback, skill-nudge counter, pre-API ``/steer`` drain as a
standalone user row after the newest tool result, run-budget wrap-up notice, tool_call
argument sanitization, interrupt-scaffold ghost-row drop, role-alternation repair),
``announce_api_call`` (verbose summary / quiet spinner) and, after the retry loop,
``apply_retry_restarts`` (consumes the ``TurnRetryState`` restart flags). Nothing here
imports ``agent.conversation_loop`` at module level (cycle)."""

from __future__ import annotations

import logging
import random
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Dict

from agent.display import KawaiiSpinner
from agent.interrupt_control import interrupt_issuer

logger = logging.getLogger("agent.conversation_loop")

ITERATION_BUDGET_WARNING_TEMPLATE = (
    "[SYSTEM NOTICE — iteration budget checkpoint] You have used {used} of {maximum} "
    "iterations. Checkpoint durable progress now, then continue the task; do not stop "
    "solely because of this warning."
)


def _maybe_inject_iteration_budget_warning(agent: Any, messages: Any) -> bool:
    """Append the opt-in one-shot warning to the newest tool result."""
    import os
    from agent.delegation_context import is_dispatcher_owned_worker_context

    # Cancellation results still need persistence, but must not urge more work.
    if getattr(agent, "_interrupt_requested", False):
        return False

    ratio = getattr(agent, "budget_warning_ratio", None)
    kanban_worker = (
        bool(os.environ.get("HERMES_KANBAN_TASK"))
        and is_dispatcher_owned_worker_context()
        and "kanban_complete" in getattr(agent, "valid_tool_names", ())
    )
    if ratio is None and kanban_worker:
        ratio = 0.9
    budget = getattr(agent, "iteration_budget", None)
    if (
        ratio is None
        or budget is None
        or budget.max_total <= 1
        or budget.max_total >= sys.maxsize
        or getattr(agent, "_iteration_budget_warning_injected", False)
        or budget.used < min(ratio * budget.max_total, budget.max_total - 1)
    ):
        return False
    notice = ITERATION_BUDGET_WARNING_TEMPLATE.format(
        used=budget.used, maximum=budget.max_total
    )
    if kanban_worker:
        notice += (
            " While tools are still available, call kanban_complete only if all task "
            "requirements are verified, or kanban_request_review if it is ready for "
            "review; otherwise persist a kanban_comment handoff and "
            "continue. A diff or commit alone is not completion evidence."
        )
    # Only the current tool-result tail is mutable; an older turn may already be cached.
    from agent.context_compressor import _DB_PERSISTED_MARKER
    if (not messages or messages[-1].get("role") != "tool"
            or messages[-1].get(_DB_PERSISTED_MARKER)):
        return False
    message = messages[-1]
    content = message.get("content", "")
    if isinstance(content, str):
        message["content"] = content + f"\n\n{notice}"
    elif isinstance(content, list) or content is None:
        message["content"] = [*(content or []), {"type": "text", "text": notice}]
    else:
        return False
    agent._iteration_budget_warning_injected = True
    return True


@dataclass
class IterationPrep:
    """Always ``action == "fallthrough"``. ``messages`` is the (possibly filtered) transcript
    and ``request_logger`` the per-request logger the caller keeps using."""

    action: str
    messages: Any
    request_logger: Any
    current_turn_user_idx: Any


def prepare_iteration(
    agent: Any, *, messages: Any, api_call_count: Any, user_message: Any = None, current_turn_user_idx: Any = None,
) -> IterationPrep:
    """Prepare ``messages`` for this iteration in the original order. Every mutation here is
    cache-safe by construction: steer text is appended as a new (not yet persisted) user row, the ghost-row
    filter only drops hidden scaffold placeholders, and repair runs BEFORE the request build."""
    from agent.conversation_loop import (
        _INTERRUPT_SCAFFOLD_MARKER, _maybe_inject_run_budget_wrapup
    )

    # Fire step_callback for gateway hooks (agent:step event).
    if agent.step_callback is not None:
        try:
            agent.step_callback(api_call_count, _previous_tool_round(messages))
        except Exception as _step_err:
            logger.debug("step_callback error (iteration %s): %s", api_call_count, _step_err)

    # One-shot run-budget wrap-up notice at 80% of agent.run_budget_seconds, appended to the
    # newest tool result; off with no budget.
    if getattr(agent, "run_budget_seconds", None):
        _maybe_inject_run_budget_wrapup(agent, messages)

    # Appended to the newest tool result; never a synthetic user/system row.
    _maybe_inject_iteration_budget_warning(agent, messages)

    request_logger = getattr(agent, "logger", None) or logger  # same name as the origin module

    # Drop legacy hidden assistant placeholders carrying the raw interrupt scaffold
    # before repair: replayed, the model echoes/self-replicates.
    def _is_scaffold_ghost(msg: Dict[str, Any]) -> bool:
        return (
            msg.get("display_kind") == "hidden"
            and msg.get("role") == "assistant"
            and any(
                isinstance(msg.get(k), str) and msg[k].strip() == _INTERRUPT_SCAFFOLD_MARKER
                for k in ("content", "api_content")
            )
        )

    messages = [msg for msg in messages if not _is_scaffold_ghost(msg)]

    return IterationPrep(
        action="fallthrough", messages=messages, request_logger=request_logger,
        current_turn_user_idx=current_turn_user_idx,
    )


def _previous_tool_round(messages: Any) -> list:
    """The newest assistant tool_calls batch with each call's result, for ``agent:step``."""
    for _idx, _m in enumerate(reversed(messages)):
        if _m.get("role") == "assistant" and _m.get("tool_calls"):
            _results_by_id = {}
            for _tm in messages[len(messages) - _idx:]:
                if _tm.get("role") != "tool":
                    break
                _tcid = _tm.get("tool_call_id")
                if _tcid:
                    _results_by_id[_tcid] = _tm.get("content", "")
            return [
                {
                    "name": tc["function"]["name"],
                    "result": _results_by_id.get(tc.get("id")),
                    "arguments": tc["function"].get("arguments"),
                }
                for tc in _m["tool_calls"]
                if isinstance(tc, dict)
            ]
    return []




@dataclass
class ApiCallAnnouncement:
    """Always ``action == "fallthrough"``; ``thinking_spinner`` is the started raw spinner or
    None (TUI widget / streaming consumers / verbose mode)."""

    action: str
    thinking_spinner: Any


def announce_api_call(
    agent: Any, *, messages: Any, api_messages: Any, api_call_count: Any, approx_tokens: Any,
    total_chars: Any,
) -> ApiCallAnnouncement:
    """Print the request summary (verbose) or start the quiet-mode thinking indicator."""
    thinking_spinner = None
    if not agent.quiet_mode:
        agent._vprint(f"\n{agent.log_prefix}🔄 Making API call #{api_call_count}/{agent.max_iterations}...")
        agent._vprint(f"{agent.log_prefix}   📊 Request size: {len(api_messages)} messages, ~{approx_tokens:,} tokens (~{total_chars:,} chars)")
        agent._vprint(f"{agent.log_prefix}   🔧 Available tools: {len(agent.tools) if agent.tools else 0}")
    else:
        # Animated thinking spinner in quiet mode
        face = random.choice(KawaiiSpinner.get_thinking_faces())
        verb = random.choice(KawaiiSpinner.get_thinking_verbs())
        if agent.thinking_callback:
            # CLI TUI mode: use prompt_toolkit widget instead of raw spinner
            # (works in both streaming and non-streaming modes)
            agent.thinking_callback(f"{face} {verb}...")
        elif not agent._has_stream_consumers() and agent._should_start_quiet_spinner():
            # Raw KawaiiSpinner only when no streaming consumers and the
            # spinner output has a safe sink.
            spinner_type = random.choice(['brain', 'sparkle', 'pulse', 'moon', 'star'])
            thinking_spinner = KawaiiSpinner(f"{face} {verb}...", spinner_type=spinner_type, print_fn=agent._print_fn)
            thinking_spinner.start()

    # Log request details if verbose
    if agent.verbose_logging:
        logging.debug(f"API Request - Model: {agent.model}, Messages: {len(messages)}, Tools: {len(agent.tools) if agent.tools else 0}")
        logging.debug(f"Last message role: {messages[-1]['role'] if messages else 'none'}")
        logging.debug(f"Total message size: ~{approx_tokens:,} tokens")
    return ApiCallAnnouncement(action="fallthrough", thinking_spinner=thinking_spinner)


@dataclass
class IterationStart:
    """``action``: ``"fallthrough"`` (run the iteration) or ``"break"`` (turn ends: interrupt,
    review input budget or iteration budget exhausted — ``_turn_exit_reason`` set)."""

    action: str
    original_user_message: Any
    api_call_count: Any
    interrupted: Any
    _turn_exit_reason: Any


def begin_iteration(
    agent: Any, *, messages: Any, conversation_history: Any, original_user_message: Any,
    api_call_count: Any, interrupted: Any, _turn_exit_reason: Any,
) -> IterationStart:
    """Iteration entry in the original order: apply a pending redirect, reset the checkpoint
    dedup, then the interrupt / review-budget / iteration-budget exits. ``api_call_count`` is
    incremented here (the grace call consumes its flag instead of the budget)."""
    def _verdict(action: str) -> IterationStart:
        return IterationStart(
            action=action, original_user_message=original_user_message,
            api_call_count=api_call_count, interrupted=interrupted,
            _turn_exit_reason=_turn_exit_reason,
        )

    if agent._interrupt_requested:
        interrupted = True
        _issuer = interrupt_issuer(agent)
        _turn_exit_reason = f"interrupted_by_system({_issuer})" if _issuer else "interrupted_by_user"
        if not agent.quiet_mode:
            agent._safe_print("\n⚡ Breaking out of tool loop due to interrupt...")
        return _verdict("break")

    api_call_count += 1
    agent._api_call_count = api_call_count
    agent._touch_activity(f"starting API call #{api_call_count}")

    # Grace call: budget exhausted but the model gets one more call. Consume the
    # flag so the loop exits after this iteration regardless of outcome.
    if agent._budget_grace_call:
        # Exhaustion retains one toolless grace call regardless of whether an opt-in
        # checkpoint was emitted; the warning never extends the hard budget.
        agent._budget_grace_call = False
    elif not agent.iteration_budget.consume():
        _turn_exit_reason = "budget_exhausted"
        if not agent.quiet_mode:
            agent._safe_print(f"\n⚠️  Iteration budget exhausted ({agent.iteration_budget.used}/{agent.iteration_budget.max_total} iterations used)", diagnostic=True)
        return _verdict("break")
    return _verdict("fallthrough")


@dataclass
class RetryRestartVerdict:
    """``action``: ``"fallthrough"`` (a response is ready — process it), ``"continue"``
    (a restart flag re-issues the iteration: redirect / compressed / rebuilt-for-fallback /
    length continuation) or ``"break"`` (turn ends: interrupted, non-actionable compaction
    handoff, or every retry exhausted without a response)."""

    action: str
    current_turn_user_idx: Any
    final_response: Any
    retry_count: Any
    restart_count: Any
    api_call_count: Any
    _preflight_compression_blocked: Any
    _turn_exit_reason: Any


def apply_retry_restarts(
    agent: Any, *, _retry: Any, response: Any, interrupted: Any, messages: Any,
    conversation_history: Any, user_message: Any, api_kwargs: Any, current_turn_user_idx: Any,
    final_response: Any, retry_count: Any, max_retries: Any, api_call_count: Any,
    restart_count: Any, length_continue_retries: Any,
    _preflight_compression_blocked: Any, _turn_exit_reason: Any,
) -> RetryRestartVerdict:
    """Consume the ``TurnRetryState`` restart flags after the retry loop, in the original
    priority order. Refunds the iteration budget/count for restarts that produced no valid
    assistant item; ``restart_with_rebuilt_messages`` is the single consumer that clears
    ``_preflight_compression_blocked`` so the fallback gets a fresh preflight (#84733).

    The two refunding restart paths (redirect and rebuilt-for-fallback) are bounded by
    ``max_retries`` via ``restart_count`` (a per-turn accumulator) so a runaway
    interrupt/redirect that keeps re-arming a restart flag cannot refund the budget
    forever and hold the turn lease indefinitely."""

    from agent.conversation_loop import (
        _HANDOFF_SKIP_FINAL_RESPONSE, _should_skip_model_call_for_reference_handoff
    )

    def _verdict(action: str) -> RetryRestartVerdict:
        return RetryRestartVerdict(
            action=action, current_turn_user_idx=current_turn_user_idx,
            final_response=final_response, retry_count=retry_count, restart_count=restart_count,
            api_call_count=api_call_count,
            _preflight_compression_blocked=_preflight_compression_blocked,
            _turn_exit_reason=_turn_exit_reason,
        )

    if _retry.restart_with_redirected_messages:
        restart_count += 1
        if restart_count > max_retries:
            # A redirect/interrupt keeps re-arming this flag: stop refunding the iteration
            # budget and re-issuing the same logical iteration, or a runaway turn holds the
            # turn lease indefinitely (redirect restarts previously had no bound).
            _turn_exit_reason = "redirect_restart_limit_exceeded"
            logger.warning(
                "Redirected-message restart limit (%s) exceeded; ending turn instead of "
                "refunding the iteration budget indefinitely.",
                max_retries,
            )
            # The correction that tripped the cap was never applied; hand it back as the
            # next user turn (result["pending_steer"]) instead of losing it to clear_interrupt().
            _unapplied = agent._drain_pending_redirect()
            if _unapplied:
                agent.steer(_unapplied)
            return _verdict("break")
        # Cancelled request produced no valid assistant item: reuse the same logical
        # iteration after the outer loop appends partial context + correction.
        api_call_count -= 1
        agent.iteration_budget.refund()
        _retry.restart_with_redirected_messages = False
        return _verdict("continue")

    if interrupted:
        _issuer = interrupt_issuer(agent)
        _turn_exit_reason = (
            f"interrupted_during_api_call({_issuer})" if _issuer else "interrupted_during_api_call"
        )
        return _verdict("break")

    if _retry.restart_with_rebuilt_messages:
        restart_count += 1
        if restart_count > max_retries:
            # A stall/failure keeps re-escalating to the fallback chain: stop refunding the
            # iteration budget and re-issuing, or a runaway turn holds the turn lease
            # indefinitely (rebuilt restarts previously had no bound).
            _turn_exit_reason = "rebuilt_restart_limit_exceeded"
            logger.warning(
                "Rebuilt-message restart limit (%s) exceeded; ending turn instead of "
                "refunding the iteration budget indefinitely.",
                max_retries,
            )
            return _verdict("break")
        # A stall/failure escalated to the fallback chain: re-issue against the
        # active fallback provider, refunding budget/count for the stalled attempt.
        api_call_count -= 1
        agent.iteration_budget.refund()
        _retry.restart_with_rebuilt_messages = False
        # Failover shrank the compressor window: clear the preflight block so
        # preflight re-runs before the first fallback call (single consumer).
        _preflight_compression_blocked = False
        return _verdict("continue")

    if _retry.restart_with_length_continuation:
        # Boost output budget per retry: 2×, 4×, 8×, 16× base, capped at 32 768, via
        # _ephemeral_max_output_tokens. Keep a larger original provider/model
        # default as the floor so retries never downshift.
        _boost = (agent.max_tokens or 4096) * (2 ** length_continue_retries)
        _requested_cap = agent._requested_output_cap_from_api_kwargs(api_kwargs)
        if _requested_cap is not None:
            _boost = max(_boost, _requested_cap)
        _boost_cap = max(32768, _requested_cap or 0)
        agent._ephemeral_max_output_tokens = min(_boost, _boost_cap)
        return _verdict("continue")

    # All retries may exhaust with `response` still None; break out cleanly.
    if response is None:
        _turn_exit_reason = "all_retries_exhausted_no_response"
        agent._emit_diagnostic_status("❌ The model provider didn't answer after all retries. Send /retry, or switch models with /model.")
        return _verdict("break")
    return _verdict("fallthrough")
