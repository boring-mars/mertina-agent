# Ported from hermes-agent agent/turn_failure_copy.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""User-facing copy for terminal turn outcomes: retries exhausted, a non-retryable rejection, and
the iteration-summary failure text."""

from __future__ import annotations

from typing import Any

from mertina.agent.error_classifier import FailoverReason


def provider_label_for(provider: Any) -> str:
    """Provider name for chat copy: the configured provider, else ``"The provider"``."""
    return str(provider or "") or "The provider"


_NEXT_STEPS_RETRY = "Wait a minute and try again."


# Lead sentence per classifier reason once retries are exhausted.
_EXHAUSTED_LEADS: dict[str, str] = {
    FailoverReason.rate_limit.value: "{label} rate-limited every one of {attempts} attempts",
    FailoverReason.overloaded.value: "{label} reported it was overloaded on all {attempts} attempts",  # noqa: E501  # upstream's message
    FailoverReason.server_error.value: "{label} returned a server error on all {attempts} attempts",
    FailoverReason.timeout.value: "{label} didn't respond in time on any of {attempts} attempts",
}


_EXHAUSTED_DEFAULT_LEAD = "{label} didn't answer after {attempts} attempts"


# Terminal copy for a non-retryable provider rejection, keyed by classifier reason.
_NONRETRYABLE_COPY: dict[str, str] = {
    FailoverReason.format_error.value: (
        "{label} rejected this request as malformed, so the model didn't answer. Check the "
        "model name and the request settings, then try again."
    ),
}


_NONRETRYABLE_DEFAULT_COPY = (
    "{label} rejected the request and retrying won't help. Check the model name, the endpoint "
    "and the API key."
)


# One-off outcome strings: deterministic loop exits that are NOT failure codes.
_ONE_OFF_COPY: dict[str, str] = {
    "max_iterations_no_summary": (
        "I ran out of steps for this turn ({limit} tool calls) before finishing, and couldn't "
        "produce a summary. Send `continue` to keep going, or raise `max_iterations` in your config."  # noqa: E501  # upstream's message
    ),
}


_SITE_COPY: dict[str, str] = {**_ONE_OFF_COPY}


def site_copy(code: str, **fields: Any) -> str:
    """Chat copy for a failure code or one-off loop outcome; unknown fields default to empty
    strings."""
    return _SITE_COPY[code].format_map(_Defaults(fields))


def exhausted_copy(reason: str, *, label: str, attempts: int, summary: str) -> str:
    """Chat copy once retries are exhausted (``max_retries_exhausted_result``)."""
    lead = _EXHAUSTED_LEADS.get(reason, _EXHAUSTED_DEFAULT_LEAD).format(
        label=label, attempts=attempts
    )
    situation = f"it looks temporarily unavailable. {_NEXT_STEPS_RETRY}"
    return f"{lead} — {situation}\n\nProvider said: {summary}"


def nonretryable_copy(
    classified: Any,
    *,
    provider: Any,
    model: Any,
    summary: str,
) -> str:
    """Chat copy for a terminal non-retryable rejection (malformed request, auth, generic 4xx)."""
    label = provider_label_for(provider)
    template = _NONRETRYABLE_COPY.get(classified.reason.value, _NONRETRYABLE_DEFAULT_COPY)
    body = template.format(
        label=label,
        model=model,
    )
    return f"{body}\n\nProvider said: {summary}"


class _Defaults(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""
