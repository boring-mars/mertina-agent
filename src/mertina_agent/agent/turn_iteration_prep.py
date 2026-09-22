"""Outer-iteration bookkeeping for the conversation turn loop.

Copied from Hermes agent/turn_iteration_prep.py (``IterationStart``,
``begin_iteration``) at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md for the pinned source and reductions.

``prepare_iteration`` (steer drain, budget notices, argument sanitization,
alternation repair), the spinner announcement, redirect handling, the review
budget and the grace call are left out.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from mertina_agent.agent.interrupt import is_interrupted
from mertina_agent.agent.iteration_budget import IterationBudget

logger = logging.getLogger(__name__)


@dataclass
class IterationStart:
    """Verdict of :func:`begin_iteration`.

    Attributes:
        action: ``"fallthrough"`` runs the iteration; ``"break"`` ends the turn
            (interrupt or exhausted budget, with ``turn_exit_reason`` set).
        api_call_count: Model calls counted so far, including this iteration's.
        interrupted: Whether the turn was stopped.
        turn_exit_reason: Diagnostic reason the loop ended, if it did.
    """

    action: Literal["fallthrough", "break"]
    api_call_count: int
    interrupted: bool
    turn_exit_reason: str


def begin_iteration(
    *,
    iteration_budget: IterationBudget,
    api_call_count: int,
    interrupted: bool,
    turn_exit_reason: str,
) -> IterationStart:
    """Enter an iteration: honour a stop request, then count and budget the model call."""

    def _verdict(action: Literal["fallthrough", "break"]) -> IterationStart:
        return IterationStart(
            action=action,
            api_call_count=api_call_count,
            interrupted=interrupted,
            turn_exit_reason=turn_exit_reason,
        )

    if is_interrupted():
        interrupted = True
        turn_exit_reason = "interrupted_by_user"
        logger.info("Breaking out of tool loop due to interrupt")
        return _verdict("break")

    api_call_count += 1
    if not iteration_budget.consume():
        turn_exit_reason = "budget_exhausted"
        logger.warning(
            "Iteration budget exhausted (%d/%d iterations used)",
            iteration_budget.used,
            iteration_budget.max_total,
        )
        return _verdict("break")
    return _verdict("fallthrough")
