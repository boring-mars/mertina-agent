"""Typed progress events an agent reports while it runs a turn.

Hermes reports progress through a dozen positional callbacks (stream deltas,
``tool.started`` / ``tool.completed`` progress, status lines). Mertina delivers
the same moments as typed events through a single ``event_callback``. Every
turn ends with exactly one of :class:`RunCompleted`, :class:`RunStopped` or
:class:`RunFailed`.

Adapted from the callback contract in Hermes agent/tool_executor.py and
agent/stream_delivery.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TextDelta:
    """A piece of the model's visible answer, delivered while it streams.

    Attributes:
        text: The new text, to be appended to what was delivered before.
    """

    text: str


@dataclass(frozen=True)
class ToolGenerationStarted:
    """The model started writing a tool call; its arguments are still streaming.

    Attributes:
        name: Tool name.
    """

    name: str


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
    """A turn ended without being stopped or failing, possibly with a partial answer.

    Attributes:
        final_response: Text delivered to the user, if any.
        completed: Whether the turn ended with a normal answer.
        turn_exit_reason: Diagnostic reason the loop ended.
    """

    final_response: str | None
    completed: bool
    turn_exit_reason: str


@dataclass(frozen=True)
class RunStopped:
    """A turn ended because it was asked to stop; its history is closed and reusable.

    Attributes:
        final_response: Partial text or cancellation note, if any.
        turn_exit_reason: Where the stop took effect.
    """

    final_response: str | None
    turn_exit_reason: str


@dataclass(frozen=True)
class RunFailed:
    """A turn ended because of an error; its history is closed and reusable.

    Attributes:
        error: Safe description of the failure, without credentials or prompts.
        final_response: Explanation delivered to the user.
        turn_exit_reason: Category of the failure.
    """

    error: str
    final_response: str | None
    turn_exit_reason: str


type AgentEvent = (
    TextDelta
    | ToolGenerationStarted
    | ToolCallStarted
    | ToolCallFinished
    | RetryScheduled
    | RunCompleted
    | RunStopped
    | RunFailed
)
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
