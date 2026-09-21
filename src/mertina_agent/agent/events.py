"""Typed progress events an agent reports while it runs a turn.

Hermes reports progress through a dozen positional callbacks (stream deltas,
``tool.started`` / ``tool.completed`` progress, status lines). Mertina delivers
the same moments as typed events through a single ``event_callback``. This
module holds the events available at this checkpoint; streaming events arrive
with the streaming transport.

Adapted from the callback contract in Hermes agent/tool_executor.py and
agent/stream_delivery.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCallStarted:
    """A tool call is about to run.

    Attributes:
        call_id: ID the model gave the call; distinguishes concurrent calls.
        name: Tool name.
    """

    call_id: str
    name: str


@dataclass(frozen=True)
class ToolCallFinished:
    """A tool call produced its result.

    Attributes:
        call_id: ID the model gave the call.
        name: Tool name.
        duration_s: Wall-clock time spent running the tool.
        is_error: Whether the result reports a failure.
    """

    call_id: str
    name: str
    duration_s: float
    is_error: bool


@dataclass(frozen=True)
class RetryScheduled:
    """A failed model call will be retried after a backoff.

    Consumers that displayed partial output of the failed attempt should discard
    it: the retry starts the response over.

    Attributes:
        attempt: Number of the attempt that failed, starting at 1.
        max_attempts: Attempts allowed for this model call.
        wait_s: Backoff before the next attempt.
        reason: Safe failure category, e.g. ``"http 503"`` or ``"invalid_response"``.
    """

    attempt: int
    max_attempts: int
    wait_s: float
    reason: str


@dataclass(frozen=True)
class RunCompleted:
    """A turn ended, whether with an answer, a partial result or a failure.

    Attributes:
        final_response: Text delivered to the user, if any.
        completed: Whether the turn ended with a normal answer.
        turn_exit_reason: Diagnostic reason the loop ended.
    """

    final_response: str | None
    completed: bool
    turn_exit_reason: str


type AgentEvent = ToolCallStarted | ToolCallFinished | RetryScheduled | RunCompleted
type EventCallback = Callable[[AgentEvent], None]


def emit_event(callback: EventCallback | None, event: AgentEvent) -> None:
    """Deliver ``event`` if a callback is set; a failing callback is logged, never fatal.

    Progress reporting is observational: a broken consumer must not abort the
    turn it is observing.
    """
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        logger.warning("Event callback failed for %s", type(event).__name__, exc_info=True)
