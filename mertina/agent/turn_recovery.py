# Ported from hermes-agent agent/turn_recovery.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Attempt logging, backoff, interrupt handling and the terminal results for the conversation
turn's inner retry loop. Logger name stays ``agent.conversation_loop`` (caplog pins).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from mertina.agent.error_classifier import FailoverReason
from mertina.agent.message_sanitization import (
    close_interrupted_tool_sequence,
)
from mertina.agent.turn_failure_copy import (
    exhausted_copy,
    nonretryable_copy,
    provider_label_for,
)

logger = logging.getLogger("mertina.agent.conversation_loop")


def _vlines(agent: Any, *lines: str) -> None:
    """Force-``_vprint`` each line prefixed with ``agent.log_prefix``."""
    for line in lines:
        agent._vprint(f"{agent.log_prefix}{line}", force=True, diagnostic=True)


def _blines(agent: Any, *lines: str) -> None:
    """``_buffer_vprint`` each line (surfaces only if every retry+fallback exhausts)."""
    for line in lines:
        agent._buffer_vprint(line)


def _failed_turn_result(
    final_response: str, messages: Any, api_call_count: int, error: str
) -> dict[str, Any]:
    """Base failed-turn result dict shared by the two terminal paths."""
    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "failed": True,
        "error": error,
    }


# Terminal status label per non-retryable reason (default names the HTTP status).
_NONRETRYABLE_LABELS: dict[FailoverReason, str] = {}


def nonretryable_client_error_result(
    agent: Any,
    api_error: Exception,
    classified: Any,
    *,
    status_code: int | None,
    messages: list[dict[str, Any]],
    api_call_count: int,
    provider: Any,
    base_url: Any,
    model: Any,
) -> dict[str, Any]:
    """Terminal path for a non-retryable 4xx: flush the retry trace, emit the status line,
    build the result."""
    # Terminal — flush buffered context so the user sees what was tried before the abort.
    agent._flush_status_buffer()
    # Summarize once: Cloudflare/proxy HTML pages and raw provider bodies must be
    # collapsed here or they leak verbatim via the ``error`` field.
    _nonretryable_summary = agent._summarize_api_error(api_error)
    _plabel = provider_label_for(provider)
    _label = _NONRETRYABLE_LABELS.get(
        classified.reason, f"{_plabel} rejected the request and retrying won't help"
    )
    agent._emit_diagnostic_status(f"❌ {_label}: {_nonretryable_summary}")
    # The endpoint/status trace is developer detail: verbose only (the log has it always).
    if getattr(agent, "verbose_logging", False):
        _vlines(
            agent,
            f"   🔌 Provider: {provider}  Model: {model}  (HTTP {status_code})",
            f"   🌐 Endpoint: {base_url}",
        )
    logger.error("%sNon-retryable client error: %s", agent.log_prefix, api_error)
    # Every surface reads final_response; the CLI hint lines above never reach chat.
    _final_response = nonretryable_copy(
        classified,
        provider=provider,
        model=model,
        summary=_nonretryable_summary,
    )
    result = _failed_turn_result(_final_response, messages, api_call_count, _nonretryable_summary)
    # Same verdict fields as the max-retries path: without them the UI descriptor
    # (agent/error_surface.py) reads a rejected OAuth token as a retryable
    # "Provider error" and offers Retry instead of a re-login.
    result.update(
        {
            "failure_reason": classified.reason.value,
            "failure_retryable": bool(classified.retryable),
        }
    )
    return result


def max_retries_exhausted_result(
    agent: Any,
    api_error: Exception,
    classified: Any,
    *,
    max_retries: int,
    is_rate_limited: bool,
    api_messages: Any,
    messages: list[dict[str, Any]],
    api_call_count: int,
    provider: Any,
    base_url: Any,
    model: Any,
) -> dict[str, Any]:
    """Terminal path once retries are exhausted: flush the trace, emit the rate-limit /
    generic status, build the result with ``failure_reason`` / ``failure_retryable``."""
    agent._flush_status_buffer()
    _final_summary = agent._summarize_api_error(api_error)
    if is_rate_limited:
        agent._emit_diagnostic_status(
            f"❌ Rate limited after {max_retries} retries — {_final_summary}"
        )
    else:
        agent._emit_diagnostic_status(
            f"❌ API failed after {max_retries} retries — {_final_summary}"
        )
    _vlines(agent, f"   💀 Final error: {_final_summary}")

    logger.error(
        "%sAPI call failed after %s retries. %s | provider=%s model=%s msgs=%s",
        agent.log_prefix,
        max_retries,
        _final_summary,
        provider,
        model,
        len(api_messages),
    )
    # Every surface reads final_response (the 💡 lines above are CLI-only), so the chat
    # text carries the plain what-happened + next step itself.
    _final_response = exhausted_copy(
        classified.reason.value,
        label=provider_label_for(provider),
        attempts=max_retries,
        summary=_final_summary,
    )
    result = _failed_turn_result(_final_response, messages, api_call_count, _final_summary)
    result.update(
        {
            # Classified reason so callers (kanban worker in cli.py) can tell a quota wall
            # (``rate_limit`` / ``billing``) from a task failure.
            "failure_reason": classified.reason.value,
            # The classifier's own retry verdict — UI surfaces use this, not the reason string.
            "failure_retryable": bool(classified.retryable),
        }
    )
    return result


def log_api_error_attempt(
    agent: Any,
    api_error: Exception,
    *,
    retry_count: int,
    max_retries: int,
    status_code: int | None,
    elapsed_time: float,
    api_messages: Any,
    retryable: bool = True,
) -> tuple[str, str, Any, Any, Any]:
    """Log one failed API attempt (warning + buffered retry trace); the buffer only surfaces
    if every retry exhausts. Returns ``(error_type, error_msg, provider, base_url, model)``.

    ``retryable=False`` (the classifier's verdict, e.g. a 401 on a static-key route) is
    named on the line: a bare ``attempt 1/3`` promises a second attempt that never comes
    and sends readers hunting for a retry bug (#73237)."""
    error_type = type(api_error).__name__
    error_msg = str(api_error).lower()
    _error_summary = agent._summarize_api_error(api_error)
    _attempt = f"attempt {retry_count}/{max_retries}" + ("" if retryable else ", not retryable")
    logger.warning(
        "API call failed (%s) error_type=%s %s summary=%s",
        _attempt,
        error_type,
        agent._client_log_context(),
        _error_summary,
    )

    _provider = getattr(agent, "provider", "unknown")
    _base = getattr(agent, "base_url", "unknown")
    _model = getattr(agent, "model", "unknown")
    _blines(agent, f"⚠️  {_attempt[0].upper()}{_attempt[1:]} failed: {_error_summary}")
    # Exception class, endpoint, raw body and message count are developer detail: verbose only.
    if getattr(agent, "verbose_logging", False):
        _status_code_str = f" [HTTP {status_code}]" if status_code else ""
        _blines(
            agent,
            f"   🔌 {error_type}{_status_code_str}  Provider: {_provider}  Model: {_model}",
            f"   🌐 Endpoint: {_base}",
        )
        if status_code and status_code < 500:
            _err_body = getattr(api_error, "body", None)
            _err_body_str = str(_err_body)[:300] if _err_body else None
            if _err_body_str:
                _blines(agent, f"   📋 Details: {_err_body_str}")
        _blines(
            agent,
            f"   ⏱️  Elapsed: {elapsed_time:.2f}s  Context: {len(api_messages)} msgs",
        )

    return error_type, error_msg, _provider, _base, _model


def abort_turn_on_interrupt(
    agent: Any,
    messages: list[dict[str, Any]],
    api_call_count: int,
    *,
    abort_message: str,
    interrupt_text: str,
) -> dict[str, Any]:
    """Announce ``abort_message``, close any open tool sequence with ``interrupt_text``,
    clear the interrupt and return the ``interrupted`` result dict."""
    _vlines(agent, f"⚡ {abort_message}")
    close_interrupted_tool_sequence(messages, interrupt_text)
    agent.clear_interrupt()
    return {
        "final_response": interrupt_text,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "interrupted": True,
    }


def interruptible_backoff_sleep(
    agent: Any,
    wait_time: float,
    *,
    messages: list[dict[str, Any]],
    api_call_count: int,
    abort_message: str,
    interrupt_text: str,
) -> dict[str, Any] | None:
    """Sleep ``wait_time`` in 200 ms slices so interrupts are honoured promptly.

    On interrupt return the ``interrupted`` result dict; ``None`` when the wait completed."""
    sleep_end = time.time() + wait_time
    while time.time() < sleep_end:
        if agent._interrupt_requested:
            return abort_turn_on_interrupt(
                agent,
                messages,
                api_call_count,
                abort_message=abort_message,
                interrupt_text=interrupt_text,
            )
        time.sleep(0.2)
    return None


def compute_error_backoff(
    agent: Any,
    api_error: Exception,
    *,
    retry_count: int,
    max_retries: int,
    is_rate_limited: bool,
) -> float:
    """Pick the wait before the next API retry and announce it. Retry-After wins for
    rate limits and any other retryable error (capped at 600s: Anthropic Tier 1 buckets
    reset in ~171s, so a 120s cap re-tripped the limit); otherwise jittered backoff.
    Normal retries are buffered; long provider cooldowns surface immediately."""
    # Imported lazily so tests that patch ``agent.retry_utils.jittered_backoff`` intercept.
    from mertina.agent.retry_utils import (
        jittered_backoff,
        parse_retry_after_seconds,
    )

    # Respect Retry-After on every retryable provider error, not just 429s. Retryable
    # 5xx responses (e.g. Cloudflare 520/524) also carry the header or a structured
    # ``retry_after`` problem-detail body field; ignoring either turns an origin
    # outage into a retry storm.
    _retry_after = parse_retry_after_seconds(
        getattr(getattr(api_error, "response", None), "headers", None)
    )
    if _retry_after is None:
        _error_body = getattr(api_error, "body", None)
        if isinstance(_error_body, dict):
            # Some providers nest it as error.retry_after (the same unwrap
            # extract_api_error_context uses), others put it at the top level.
            _nested = _error_body.get("error")
            _payload = _nested if isinstance(_nested, dict) else _error_body
            _retry_after = parse_retry_after_seconds(_payload.get("retry_after"))
    if _retry_after is not None:
        # Cap at 10 minutes. Anthropic Tier 1 input-token buckets reset in ~171s, so a 120s cap
        # caused us to retry before the actual reset window and re-trip the limit. 600s covers all
        # realistic provider reset windows while still rejecting pathological values. (#26293)
        _retry_after = min(_retry_after, 600)
        if _retry_after <= 0:
            # A zero/expired cooldown (retry-after: 0, or an HTTP-date in the
            # past, which the parser clamps to 0.0) carries no usable wait —
            # treat it as absent so we never hot-loop the provider.
            _retry_after = None
    wait_time = (
        _retry_after
        if _retry_after is not None
        else jittered_backoff(retry_count, base_delay=2.0, max_delay=60.0)
    )
    _adaptive = is_rate_limited
    _wait_reason = "Rate limited"
    if _adaptive:
        _rate_limit_status = (
            f"⏱️ {_wait_reason}. Waiting {wait_time:.1f}s "
            f"(attempt {retry_count + 1}/{max_retries})..."
        )
        agent._buffer_diagnostic_status(_rate_limit_status)
    else:
        _retry_status = f"⏳ Retrying in {wait_time:.1f}s (attempt {retry_count}/{max_retries})..."
        if _retry_after is not None and _retry_after > 60:
            # A 5xx Retry-After can now reach the 600s cap; buffering that wait
            # would leave the user silent for minutes, so surface long provider
            # cooldowns immediately.
            agent._emit_diagnostic_status(_retry_status)
        else:
            agent._buffer_diagnostic_status(_retry_status)
    # The buffered line only replays if every retry fails; the live status
    # line is the one thing the user sees meanwhile. Name the wait there so a
    # 60s backoff after a 5xx is not an anonymous spinner — this is transient
    # (rewritten by the next frame, cleared on recovery), so it does not add
    # the transcript chatter the buffer exists to avoid.
    _live_reason = "waiting on provider —"
    agent._emit_diagnostic_wait(
        f"⏳ {_live_reason} retrying in {wait_time:.0f}s (attempt {retry_count}/{max_retries})"
    )
    logger.warning(
        "Retrying API call in %ss (attempt %s/%s) %s error=%s",
        wait_time,
        retry_count,
        max_retries,
        agent._client_log_context(),
        api_error,
    )
    return wait_time


@dataclass
class ClassifiedErrorVerdict:
    """Outcome of ``route_classified_error``. ``action``: ``"fallthrough"`` (proceed to
    client-error / backoff handling). The remaining fields are loop locals the router rebound
    or computed."""

    action: str
    result: dict[str, Any] | None
    retry_count: int
    max_retries: int
    is_rate_limited: bool


_RATE_LIMIT_REASONS = frozenset(
    {
        FailoverReason.rate_limit,
    }
)


def route_classified_error(
    agent: Any,
    api_error: Exception,
    classified: Any,
    *,
    retry_count: int,
    max_retries: int,
) -> ClassifiedErrorVerdict:
    """Mark rate limits (``is_rate_limited``) for the backoff status and the terminal copy."""
    is_rate_limited = False

    def _verdict(action: str, result: dict[str, Any] | None = None) -> ClassifiedErrorVerdict:
        return ClassifiedErrorVerdict(
            action=action,
            result=result,
            retry_count=retry_count,
            max_retries=max_retries,
            is_rate_limited=is_rate_limited,
        )

    is_rate_limited = classified.reason in _RATE_LIMIT_REASONS
    # Upstream capacity 429: normal retry logic will typically succeed.
    return _verdict("fallthrough")
