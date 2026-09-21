"""Model-call error handling for the turn's retry loop: classify, back off, or end the turn.

Copied from Hermes agent/turn_api_error.py (``handle_api_error``,
``settle_unrecovered_error``), agent/turn_recovery.py (``compute_error_backoff``,
``interruptible_backoff_sleep``, ``abort_turn_on_interrupt``) and
agent/turn_response_check.py (``retry_invalid_response``) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

The decision order is Hermes's: count the attempt, honour a stop request, end
the turn on a non-retryable error, end it when attempts are exhausted, else
back off (``Retry-After`` first) and retry. Retries never append to the history
and never consume the iteration budget. The full error classifier, credential
refresh and rotation, fallback providers, auto-recovery and context-overflow
recovery are left out.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from mertina_agent.agent.events import EventCallback, RetryScheduled, emit_event
from mertina_agent.agent.interrupt import current_interrupt_signal
from mertina_agent.agent.message_sanitization import close_interrupted_tool_sequence
from mertina_agent.agent.retry_utils import jittered_backoff, parse_retry_after_seconds
from mertina_agent.agent.transports.types import ChatMessage, Usage
from mertina_agent.agent.turn_failure_copy import (
    MODEL_REQUEST_FAILED,
    RATE_LIMITED_EXHAUSTED,
    RETRIES_EXHAUSTED,
)
from mertina_agent.agent.turn_result import ConversationResult
from mertina_agent.exceptions import ModelRequestError, ModelResponseError

logger = logging.getLogger(__name__)

# Retry-After is honoured up to 10 minutes: provider token buckets can take
# minutes to refill, and a shorter cap would retry into the same limit.
_RETRY_AFTER_CAP_S = 600.0

# Hermes's status table: 408 is retry-safe (RFC 9110), 429 is a rate limit and
# every 5xx is a server-side failure. Other 4xx are the caller's problem.
_RETRYABLE_STATUS_CODES = frozenset({408, 429})


async def interruptible_backoff_sleep(wait_s: float) -> bool:
    """Sleep ``wait_s``, returning ``True`` early if the run is asked to stop."""
    signal = current_interrupt_signal()
    if signal is None:
        await asyncio.sleep(wait_s)
        return False
    return await signal.wait(wait_s)


@dataclass(frozen=True)
class RetryPolicy:
    """How a failed model call is retried.

    Attributes:
        max_attempts: Attempts per model call, including the first.
        rng: Random source for backoff jitter; inject a seeded one in tests.
        sleep: Awaits a backoff and returns ``True`` if the run was stopped
            meanwhile; inject a recording fake in tests.
    """

    max_attempts: int = 3
    rng: random.Random = field(default_factory=random.SystemRandom)
    sleep: Callable[[float], Awaitable[bool]] = interruptible_backoff_sleep


def _is_retryable(error: ModelRequestError | ModelResponseError) -> bool:
    if isinstance(error, ModelResponseError):
        # Malformed or empty responses are usually transient provider glitches.
        return True
    if error.kind in {"timeout", "connection"}:
        return True
    status = error.status_code
    return (
        error.kind == "http"
        and status is not None
        and (status in _RETRYABLE_STATUS_CODES or status >= 500)
    )


def _failure_category(error: ModelRequestError | ModelResponseError) -> str:
    if isinstance(error, ModelResponseError):
        return "invalid_response"
    if error.status_code is not None:
        return f"{error.kind} {error.status_code}"
    return error.kind


def compute_error_backoff(
    error: ModelRequestError | ModelResponseError, *, retry_count: int, rng: random.Random
) -> float:
    """Pick the wait before the next attempt.

    ``Retry-After`` wins for any retryable error (a 5xx may carry it too), capped
    at 10 minutes; a zero or past value carries no usable wait and is ignored.
    Otherwise request errors use ``jittered_backoff(n, 2, 60)`` and malformed
    responses ``jittered_backoff(n, 5, 120)``, as in Hermes.
    """
    if isinstance(error, ModelRequestError):
        retry_after = parse_retry_after_seconds(error.retry_after)
        if retry_after is not None and retry_after > 0:
            return min(retry_after, _RETRY_AFTER_CAP_S)
        return jittered_backoff(retry_count, base_delay=2.0, max_delay=60.0, rng=rng)
    return jittered_backoff(retry_count, base_delay=5.0, max_delay=120.0, rng=rng)


def abort_turn_on_interrupt(
    messages: list[ChatMessage], api_call_count: int, *, interrupt_text: str, usage: Usage
) -> ConversationResult:
    """End the turn as interrupted, closing any open tool-result tail."""
    logger.info("%s", interrupt_text)
    close_interrupted_tool_sequence(messages, interrupt_text)
    return {
        "final_response": interrupt_text,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "failed": False,
        "interrupted": True,
        "partial": False,
        "turn_exit_reason": "interrupted_during_retry",
        "usage": usage,
    }


def _failed_result(
    messages: list[ChatMessage],
    api_call_count: int,
    *,
    final_response: str,
    error: str,
    turn_exit_reason: str,
    usage: Usage,
) -> ConversationResult:
    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "failed": True,
        "interrupted": False,
        "partial": False,
        "turn_exit_reason": turn_exit_reason,
        "usage": usage,
        "error": error,
    }


@dataclass
class ApiErrorVerdict:
    """Verdict of :func:`handle_api_error`.

    Attributes:
        action: ``"continue"`` retries the call; ``"return"`` ends the turn with ``result``.
        retry_count: Failed attempts of this model call so far.
        result: The turn result when ``action`` is ``"return"``.
    """

    action: Literal["continue", "return"]
    retry_count: int
    result: ConversationResult | None = None


async def handle_api_error(
    error: ModelRequestError | ModelResponseError,
    *,
    retry_count: int,
    policy: RetryPolicy,
    messages: list[ChatMessage],
    api_call_count: int,
    usage: Usage,
    event_callback: EventCallback | None,
) -> ApiErrorVerdict:
    """Decide whether a failed model call is retried or ends the turn.

    Error messages come from the model client and never contain credentials,
    prompts or provider bodies, so they are safe to report.
    """
    retry_count += 1
    retryable = _is_retryable(error)
    category = _failure_category(error)
    logger.warning(
        "API call failed (attempt %d/%d%s): %s",
        retry_count,
        policy.max_attempts,
        "" if retryable else ", not retryable",
        category,
    )

    signal = current_interrupt_signal()
    if signal is not None and signal.is_set():
        return ApiErrorVerdict(
            "return",
            retry_count,
            abort_turn_on_interrupt(
                messages,
                api_call_count,
                interrupt_text=f"Operation interrupted: handling API error ({category}).",
                usage=usage,
            ),
        )

    if not retryable:
        return ApiErrorVerdict(
            "return",
            retry_count,
            _failed_result(
                messages,
                api_call_count,
                final_response=MODEL_REQUEST_FAILED.format(detail=error),
                error=str(error),
                turn_exit_reason="model_request_failed",
                usage=usage,
            ),
        )

    if retry_count >= policy.max_attempts:
        rate_limited = isinstance(error, ModelRequestError) and error.status_code == 429
        template = RATE_LIMITED_EXHAUSTED if rate_limited else RETRIES_EXHAUSTED
        logger.error("Model call failed after %d attempts: %s", retry_count, category)
        return ApiErrorVerdict(
            "return",
            retry_count,
            _failed_result(
                messages,
                api_call_count,
                final_response=template.format(attempts=retry_count, detail=error),
                error=str(error),
                turn_exit_reason="all_retries_exhausted",
                usage=usage,
            ),
        )

    wait_s = compute_error_backoff(error, retry_count=retry_count, rng=policy.rng)
    logger.warning(
        "Retrying API call in %.1fs (attempt %d/%d)", wait_s, retry_count, policy.max_attempts
    )
    emit_event(
        event_callback,
        RetryScheduled(
            attempt=retry_count, max_attempts=policy.max_attempts, wait_s=wait_s, reason=category
        ),
    )
    if await policy.sleep(wait_s):
        return ApiErrorVerdict(
            "return",
            retry_count,
            abort_turn_on_interrupt(
                messages,
                api_call_count,
                interrupt_text=(
                    "Operation interrupted: retrying API call after error "
                    f"(retry {retry_count}/{policy.max_attempts})."
                ),
                usage=usage,
            ),
        )
    return ApiErrorVerdict("continue", retry_count)
