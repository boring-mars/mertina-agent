# Ported from hermes-agent agent/turn_iteration_prep.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Outer-iteration bookkeeping for the conversation turn loop, in call order:
``begin_iteration`` (interrupt / iteration-budget exits) and ``announce_api_call`` (verbose
summary). Nothing here imports ``agent.conversation_loop`` at module level (cycle)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

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
