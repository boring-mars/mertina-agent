# Ported from hermes-agent agent/turn_iteration_prep.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Outer-iteration bookkeeping for the conversation turn loop, in call order:
``begin_iteration`` (interrupt / iteration-budget exits) and ``announce_api_call`` (verbose
summary). Nothing here imports ``agent.conversation_loop`` at module level (cycle)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mertina.agent.interrupt_control import interrupt_issuer
from mertina.agent.turn_context_compaction import _reanchor

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class ApiCallAnnouncement:
    """Always ``action == "fallthrough"``."""

    action: str


def announce_api_call(
    agent: Any,
    *,
    messages: Any,
    api_call_count: Any,
) -> ApiCallAnnouncement:
    """Print the request summary (verbose)."""
    if not agent.quiet_mode:
        agent._vprint(
            f"\n{agent.log_prefix}🔄 Making API call #{api_call_count}/{agent.max_iterations}..."
        )
        agent._vprint(
            f"{agent.log_prefix}   🔧 Available tools: {len(agent.tools) if agent.tools else 0}"
        )

    # Log request details if verbose
    if agent.verbose_logging:
        logging.debug(
            f"API Request - Model: {agent.model}, Messages: {len(messages)}, Tools: {len(agent.tools) if agent.tools else 0}"  # noqa: E501  # upstream's message
        )
        logging.debug(f"Last message role: {messages[-1]['role'] if messages else 'none'}")
    return ApiCallAnnouncement(action="fallthrough")


@dataclass
class IterationStart:
    """``action``: ``"fallthrough"`` (run the iteration) or ``"break"`` (turn ends: interrupt
    or iteration budget exhausted — ``_turn_exit_reason`` set)."""

    action: str
    api_call_count: Any
    interrupted: Any
    _turn_exit_reason: Any


def begin_iteration(
    agent: Any,
    *,
    api_call_count: Any,
    interrupted: Any,
    _turn_exit_reason: Any,
) -> IterationStart:
    """Iteration entry in the original order: the interrupt / iteration-budget exits.
    ``api_call_count`` is incremented here."""

    def _verdict(action: str) -> IterationStart:
        return IterationStart(
            action=action,
            api_call_count=api_call_count,
            interrupted=interrupted,
            _turn_exit_reason=_turn_exit_reason,
        )

    if agent._interrupt_requested:
        interrupted = True
        _turn_exit_reason = "interrupted_by_user"
        if not agent.quiet_mode:
            agent._safe_print("\n⚡ Breaking out of tool loop due to interrupt...")
        return _verdict("break")

    api_call_count += 1

    if not agent.iteration_budget.consume():
        _turn_exit_reason = "budget_exhausted"
        if not agent.quiet_mode:
            agent._safe_print(
                f"\n⚠️  Iteration budget exhausted ({agent.iteration_budget.used}/{agent.iteration_budget.max_total} iterations used)",  # noqa: E501  # upstream's message
                diagnostic=True,
            )
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
    agent: Any,
    *,
    _retry: Any,
    response: Any,
    interrupted: Any,
    messages: Any,
    conversation_history: Any,
    user_message: Any,
    api_kwargs: Any,
    current_turn_user_idx: Any,
    final_response: Any,
    retry_count: Any,
    max_retries: Any,
    api_call_count: Any,
    restart_count: Any,
    length_continue_retries: Any,
    _preflight_compression_blocked: Any,
    _turn_exit_reason: Any,
) -> RetryRestartVerdict:
    """Consume the ``TurnRetryState`` restart flags after the retry loop, in the original
    priority order. Refunds the iteration budget/count for restarts that produced no valid
    assistant item; ``restart_with_rebuilt_messages`` is the single consumer that clears
    ``_preflight_compression_blocked`` so the fallback gets a fresh preflight (#84733).

    The two refunding restart paths (redirect and rebuilt-for-fallback) are bounded by
    ``max_retries`` via ``restart_count`` (a per-turn accumulator) so a runaway
    interrupt/redirect that keeps re-arming a restart flag cannot refund the budget
    forever and hold the turn lease indefinitely."""

    from mertina.agent.conversation_loop import (
        _HANDOFF_SKIP_FINAL_RESPONSE,
        _should_skip_model_call_for_reference_handoff,
    )

    def _verdict(action: str) -> RetryRestartVerdict:
        return RetryRestartVerdict(
            action=action,
            current_turn_user_idx=current_turn_user_idx,
            final_response=final_response,
            retry_count=retry_count,
            restart_count=restart_count,
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

    if _retry.restart_with_compressed_messages:
        api_call_count -= 1
        agent.iteration_budget.refund()
        # Compression restarts count toward the retry limit so a compression that
        # shrinks messages but not enough can't loop forever.
        retry_count += 1
        _retry.restart_with_compressed_messages = False
        if _should_skip_model_call_for_reference_handoff(
            # Compression rebuilt the list (tail messages are fresh compaction copies), so the
            # pre-compression index of this turn's user message is stale. Re-anchor both index trackers: the
            # api_content stamp below, the loop's injection site, and the flush's persist-override row
            # (#48677) must all target the surviving dict, not a stale position. Exact-content match first
            # so a todo-snapshot user message appended after the tail can't steal the anchor.
            messages,
            user_message,
        ):
            logger.info(
                "Skipping compressed-restart model call: reference-only "
                "handoff would be the sole active user turn (#80622)"
            )
            if not final_response:
                final_response = _HANDOFF_SKIP_FINAL_RESPONSE
            _turn_exit_reason = "compaction_handoff_not_actionable"
            return _verdict("break")
        # In-loop compression rebuilt `messages`; re-anchor the current-turn index
        # like the prologue, AFTER the handoff guard (it may re-append this turn's
        # ask). A stale anchor injects prefetch into a historical row.
        current_turn_user_idx = _reanchor(agent, messages, user_message)
        return _verdict("continue")

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
        _boost = (agent.max_tokens or 4096) * (2**length_continue_retries)
        _requested_cap = agent._requested_output_cap_from_api_kwargs(api_kwargs)
        if _requested_cap is not None:
            _boost = max(_boost, _requested_cap)
        _boost_cap = max(32768, _requested_cap or 0)
        agent._ephemeral_max_output_tokens = min(_boost, _boost_cap)
        return _verdict("continue")

    # All retries may exhaust with `response` still None; break out cleanly.
    if response is None:
        _turn_exit_reason = "all_retries_exhausted_no_response"
        agent._emit_diagnostic_status(
            "❌ The model provider didn't answer after all retries. Send /retry, or switch models with /model."
        )
        agent._persist_session(messages, conversation_history)
        return _verdict("break")
    return _verdict("fallthrough")
