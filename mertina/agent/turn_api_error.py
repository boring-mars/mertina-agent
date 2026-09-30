# Ported from hermes-agent agent/turn_api_error.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""API-call exception handler for the conversation turn's retry loop: classification, the
non-retryable client-error exit, max-retries exhaustion and the interruptible backoff.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
from dataclasses import dataclass
from typing import Any

from mertina.agent.error_classifier import (
    RETRYABLE_CLIENT_REASONS,
    classify_api_error,
)
from mertina.agent.turn_recovery import (
    abort_turn_on_interrupt,
    compute_error_backoff,
    interruptible_backoff_sleep,
    log_api_error_attempt,
    max_retries_exhausted_result,
    nonretryable_client_error_result,
    route_classified_error,
)

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class ApiErrorVerdict:
    """``action``: ``"fallthrough"`` (retry the API call after the backoff) or ``"return"``
    (``result`` is the turn's result dict). The other fields are the retry-loop locals the
    handler rebinds."""

    action: str
    messages: Any
    conversation_history: Any
    approx_tokens: Any
    retry_count: Any
    max_retries: Any
    result: dict[str, Any] | None = None


def handle_api_error(
    agent: Any,
    *,
    api_error: Any,
    messages: Any,
    api_messages: Any,
    api_kwargs: Any,
    conversation_history: Any,
    approx_tokens: Any,
    retry_count: Any,
    max_retries: Any,
    api_call_count: Any,
    api_start_time: Any,
) -> ApiErrorVerdict:
    """Classify ``api_error``, count the attempt, and end the turn or back off before the
    next one."""

    def _verdict(action: str, result: dict[str, Any] | None = None) -> ApiErrorVerdict:
        return ApiErrorVerdict(
            action=action,
            messages=messages,
            conversation_history=conversation_history,
            approx_tokens=approx_tokens,
            retry_count=retry_count,
            max_retries=max_retries,
            result=result,
        )

    status_code = getattr(api_error, "status_code", None)

    classified = classify_api_error(
        api_error,
        provider=getattr(agent, "provider", "") or "",
        model=getattr(agent, "model", "") or "",
        approx_tokens=approx_tokens,
        num_messages=len(api_messages) if api_messages else 0,
        base_url=str(getattr(agent, "base_url", "") or ""),
        api_key=getattr(agent, "api_key", None),
    )
    logger.debug(
        "Error classified: reason=%s status=%s retryable=%s compress=%s rotate=%s fallback=%s",
        classified.reason.value,
        classified.status_code,
        classified.retryable,
        classified.should_compress,
        classified.should_rotate_credential,
        classified.should_fallback,
    )
    retry_count += 1
    elapsed_time = time.time() - api_start_time

    error_type, error_msg, _provider, _base, _model = log_api_error_attempt(
        agent,
        api_error,
        retry_count=retry_count,
        max_retries=max_retries,
        status_code=status_code,
        elapsed_time=elapsed_time,
        api_messages=api_messages,
        approx_tokens=approx_tokens,
        retryable=bool(classified.retryable),
    )

    if agent._interrupt_requested:
        return _verdict(
            "return",
            abort_turn_on_interrupt(
                agent,
                messages,
                conversation_history,
                api_call_count,
                abort_message="Interrupt detected during error handling, aborting retries.",
                interrupt_text=f"Operation interrupted: handling API error ({error_type}: {agent._clean_error_message(str(api_error))}).",  # noqa: E501  # upstream's message
            ),
        )

    _ce = route_classified_error(
        agent,
        api_error,
        classified,
        retry_count=retry_count,
        max_retries=max_retries,
    )
    retry_count = _ce.retry_count
    max_retries = _ce.max_retries
    is_rate_limited = _ce.is_rate_limited
    if _ce.action != "fallthrough":
        return _verdict(_ce.action, _ce.result)

    _ue = settle_unrecovered_error(
        agent,
        api_error=api_error,
        classified=classified,
        status_code=status_code,
        error_msg=error_msg,
        is_rate_limited=is_rate_limited,
        _provider=_provider,
        _base=_base,
        _model=_model,
        messages=messages,
        api_messages=api_messages,
        api_kwargs=api_kwargs,
        conversation_history=conversation_history,
        approx_tokens=approx_tokens,
        retry_count=retry_count,
        max_retries=max_retries,
        api_call_count=api_call_count,
    )
    retry_count = _ue.retry_count
    return _verdict(_ue.action, _ue.result)


def _is_local_validation_error(api_error: Any) -> bool:
    """ValueError/TypeError are local bugs, except: UnicodeEncodeError (surrogate recovery
    path), json.JSONDecodeError (transient provider/network failure, must retry),
    ssl.SSLError (inherits OSError *and* ValueError — a TLS failure is not a local bug)
    and "NoneType is not iterable" TypeErrors (upstream shape mismatches, e.g. Codex
    response.completed.output=null — retryable)."""
    if not isinstance(api_error, (ValueError, TypeError)):
        return False
    if isinstance(api_error, (UnicodeEncodeError, json.JSONDecodeError, ssl.SSLError)):
        return False
    _text = str(api_error).lower()
    return not (
        isinstance(api_error, TypeError) and "nonetype" in _text and "not iterable" in _text
    )


@dataclass
class UnrecoveredErrorVerdict:
    """``action``: ``"fallthrough"`` (retry after the backoff) or ``"return"`` (``result`` is
    the terminal result dict). Rebinds ``retry_count``."""

    action: str
    retry_count: Any
    result: dict[str, Any] | None = None


def settle_unrecovered_error(
    agent: Any,
    *,
    api_error: Any,
    classified: Any,
    status_code: Any,
    error_msg: Any,
    is_rate_limited: Any,
    _provider: Any,
    _base: Any,
    _model: Any,
    messages: Any,
    api_messages: Any,
    api_kwargs: Any,
    conversation_history: Any,
    approx_tokens: Any,
    retry_count: Any,
    max_retries: Any,
    api_call_count: Any,
) -> UnrecoveredErrorVerdict:
    """Decide the fate of an API error: local validation / non-retryable client errors end the
    turn, so does max-retries exhaustion, else the interruptible error backoff.
    ``FailoverReason.billing`` (402) is deliberately treated as non-retryable (#31273)."""

    def _verdict(action: str, result: dict[str, Any] | None = None) -> UnrecoveredErrorVerdict:
        return UnrecoveredErrorVerdict(
            action=action,
            retry_count=retry_count,
            result=result,
        )

    # ``FailoverReason.billing`` (402) is deliberately NOT excluded: retrying only burns
    # paid requests on a depleted balance. Mirrors 401/403.
    is_local_validation_error = _is_local_validation_error(api_error)
    is_client_error = is_local_validation_error or (
        not classified.retryable
        and not classified.should_compress
        and classified.reason not in RETRYABLE_CLIENT_REASONS
    )

    if is_client_error:
        return _verdict(
            "return",
            nonretryable_client_error_result(
                agent,
                api_error,
                classified,
                status_code=status_code,
                api_kwargs=api_kwargs,
                api_messages=api_messages,
                messages=messages,
                conversation_history=conversation_history,
                api_call_count=api_call_count,
                approx_tokens=approx_tokens,
                provider=_provider,
                base_url=_base,
                model=_model,
            ),
        )

    if retry_count >= max_retries:
        return _verdict(
            "return",
            max_retries_exhausted_result(
                agent,
                api_error,
                classified,
                max_retries=max_retries,
                is_rate_limited=is_rate_limited,
                error_msg=error_msg,
                api_kwargs=api_kwargs,
                api_messages=api_messages,
                messages=messages,
                conversation_history=conversation_history,
                api_call_count=api_call_count,
                approx_tokens=approx_tokens,
                provider=_provider,
                base_url=_base,
                model=_model,
            ),
        )

    wait_time = compute_error_backoff(
        agent,
        api_error,
        retry_count=retry_count,
        max_retries=max_retries,
        is_rate_limited=is_rate_limited,
        base_url=_base,
        model=_model,
    )
    _interrupted = interruptible_backoff_sleep(
        agent,
        wait_time,
        messages=messages,
        conversation_history=conversation_history,
        api_call_count=api_call_count,
        abort_message="Interrupt detected during retry wait, aborting.",
        interrupt_text=f"Operation interrupted: retrying API call after error (retry {retry_count}/{max_retries}).",  # noqa: E501  # upstream's message
        activity_label=f"error retry backoff ({retry_count}/{max_retries})",
    )
    if _interrupted is not None:
        return _verdict("return", _interrupted)
    return _verdict("fallthrough")
