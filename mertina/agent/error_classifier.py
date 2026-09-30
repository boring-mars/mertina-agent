# Ported from hermes-agent agent/error_classifier.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""API error classification for the retry loop.

A pipeline maps an API exception to a ``ClassifiedError`` by its HTTP status code; the retry
loop consults its ``retryable`` hint instead of re-matching strings itself.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any


class FailoverReason(enum.Enum):
    """Why an API call failed — determines recovery strategy."""

    auth = "auth"  # Transient auth (401/403) — refresh/rotate
    rate_limit = "rate_limit"  # 429 or quota-based throttling — backoff then rotate
    overloaded = "overloaded"  # 503/529 — provider overloaded, backoff
    server_error = "server_error"  # 500/502 — internal server error, retry
    timeout = "timeout"  # Connection/read timeout — rebuild client + retry
    format_error = "format_error"  # 400 bad request — abort or strip + retry
    unknown = "unknown"  # Unclassifiable — retry with backoff


@dataclass
class ClassifiedError:
    """Structured classification of an API error with recovery hints."""

    reason: FailoverReason
    status_code: int | None = None
    provider: str | None = None
    model: str | None = None
    message: str = ""

    # Recovery hints — the retry loop checks these instead of re-classifying.
    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False


Verdict = dict[str, Any]


def _v(reason: FailoverReason, **hints: Any) -> Verdict:
    return {"reason": reason, **hints}


_ROTATE_FALLBACK = {"should_rotate_credential": True, "should_fallback": True}


_ABORT_FALLBACK = {"retryable": False, "should_fallback": True}


_R = FailoverReason


_V_RATE_LIMIT = _v(_R.rate_limit, **_ROTATE_FALLBACK)


_V_AUTH_ROTATE = _v(_R.auth, retryable=False, **_ROTATE_FALLBACK)


_V_AUTH_FALLBACK = _v(_R.auth, **_ABORT_FALLBACK)


_V_FORMAT_ERROR = _v(_R.format_error, **_ABORT_FALLBACK)


_V_OVERLOADED, _V_SERVER_ERROR, _V_TIMEOUT, _V_UNKNOWN = map(
    _v, (_R.overloaded, _R.server_error, _R.timeout, _R.unknown)
)


@dataclass
class _Ctx:
    """Everything the classifier stages need about one failed call."""

    error: Exception
    status_code: int | None


RETRYABLE_CLIENT_REASONS = frozenset(
    {
        FailoverReason.rate_limit,
        FailoverReason.overloaded,
    }
)


def _by_status(c: _Ctx) -> Verdict | None:
    """HTTP status code (unlisted 4xx/5xx → generic)."""
    status = c.status_code
    if status is None:
        return None
    default = (
        _V_FORMAT_ERROR if 400 <= status < 500 else _V_SERVER_ERROR if 500 <= status < 600 else None
    )
    return _STATUS_HANDLERS[status](c) if status in _STATUS_HANDLERS else default


# Stage order: HTTP status → unknown (retryable with backoff).
_STAGES: Sequence[Callable[[_Ctx], Verdict | None]] = (_by_status,)


def classify_api_error(
    error: Exception,
    *,
    provider: str = "",
    model: str = "",
) -> ClassifiedError:
    """Classify an API error into a structured recovery recommendation (see ``_STAGES``)."""
    status_code = _extract_status_code(error)
    # Copilot/GitHub Models RateLimitError may not set .status_code; force 429.
    if status_code is None and type(error).__name__ == "RateLimitError":
        status_code = 429
    body = _extract_error_body(error)
    c = _Ctx(
        error,
        status_code,
    )
    verdict = next((v for v in (stage(c) for stage in _STAGES) if v is not None), _V_UNKNOWN)
    message = _extract_message(error, body)
    base = {"status_code": status_code, "provider": provider, "model": model, "message": message}
    return ClassifiedError(**{**base, **verdict})


def _status_403(c: _Ctx) -> Verdict:
    return _V_AUTH_FALLBACK


def _status_429(c: _Ctx) -> Verdict:
    return _V_RATE_LIMIT


def _status_5xx(c: _Ctx) -> Verdict:
    return _V_SERVER_ERROR


# 401 not retryable: the client-error abort path is correct. 408 is
# retry-safe (RFC 9110 §15.5.9; proxies emit it when generation outruns the
# read window). Unlisted 4xx → format_error, 5xx → server_error.
_STATUS_HANDLERS: dict[int, Callable[[_Ctx], Verdict]] = {
    401: lambda c: _V_AUTH_ROTATE,
    403: _status_403,
    408: lambda c: _V_TIMEOUT,
    429: _status_429,
    500: _status_5xx,
    502: _status_5xx,
    503: lambda c: _V_OVERLOADED,
    529: lambda c: _V_OVERLOADED,
}


def _error_obj(body: Any) -> dict:
    """``body["error"]`` when it is a dict, else ``{}``."""
    err = body.get("error") if isinstance(body, dict) else None
    return err if isinstance(err, dict) else {}


def _body_message_candidates(body: dict) -> Iterator[Any]:
    """Body message fields in priority order (OpenAI, flat, litellm/Bedrock proxy, FastAPI
    shapes)."""
    yield _error_obj(body).get("message")
    yield body.get("message")
    yield body.get("errorMessage")
    args = body.get("errorArgs")
    yield args.get("reason") if isinstance(args, dict) else None
    # FastAPI/Starlette relays and the Codex gateway answer {"detail": "..."} (or a nested
    # OpenAI-ish object) (#81558).
    detail = body.get("detail")
    yield (
        detail.get("message")
        if isinstance(detail, dict)
        else detail
        if isinstance(detail, str)
        else None
    )


def _from_cause_chain(error: Exception, pick: Callable[[Any], Any], default: Any) -> Any:
    """First non-None ``pick(exc)`` over the error and its __cause__/__context__ chain (max 5
    deep)."""
    current = error
    for _ in range(5):
        found = pick(current)
        if found is not None:
            return found
        cause = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if cause is None or cause is current:
            break
        current = cause
    return default


def _status_of(exc: Any) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    code = getattr(exc, "status", None)  # some SDKs use .status
    return code if isinstance(code, int) and 100 <= code < 600 else None


def _body_of(exc: Any) -> dict | None:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return body
    response = getattr(exc, "response", None)
    try:
        json_body = response.json() if response is not None else None
    except Exception:
        return None
    return json_body if isinstance(json_body, dict) else None


def _extract_status_code(error: Exception) -> int | None:
    """HTTP status code from the error or its cause chain."""
    return _from_cause_chain(error, _status_of, None)


def _extract_error_body(error: Exception) -> dict:
    """Structured error body from an SDK exception or its cause chain."""
    return _from_cause_chain(error, _body_of, {})


def _extract_message(error: Exception, body: dict) -> str:
    """Extract the most informative error message (structured body first)."""
    msg = next(
        (m for m in _body_message_candidates(body or {}) if isinstance(m, str) and m.strip()), None
    )
    return (msg.strip() if msg else str(error))[:500]
