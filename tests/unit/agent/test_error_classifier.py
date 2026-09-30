"""The error classifier: an HTTP status decides the reason and whether a retry can help."""

import httpx
import openai
import pytest

from mertina.agent.error_classifier import (
    RETRYABLE_CLIENT_REASONS,
    FailoverReason,
    classify_api_error,
)

_REQUEST = httpx.Request("POST", "http://fake.test/v1/chat/completions")


def _status_error(status: int, body: object = None) -> openai.APIStatusError:
    response = httpx.Response(status, request=_REQUEST)
    return openai.APIStatusError("boom", response=response, body=body)


@pytest.mark.parametrize(
    ("status", "reason", "retryable"),
    [
        (400, FailoverReason.format_error, False),
        (401, FailoverReason.auth, False),
        (403, FailoverReason.auth, False),
        (404, FailoverReason.format_error, False),
        (408, FailoverReason.timeout, True),
        (413, FailoverReason.format_error, False),
        (418, FailoverReason.format_error, False),
        (429, FailoverReason.rate_limit, True),
        (500, FailoverReason.server_error, True),
        (502, FailoverReason.server_error, True),
        (503, FailoverReason.overloaded, True),
        (504, FailoverReason.server_error, True),
        (529, FailoverReason.overloaded, True),
    ],
)
def test_the_status_code_decides(status: int, reason: FailoverReason, retryable: bool) -> None:
    classified = classify_api_error(_status_error(status), provider="acme", model="m")

    assert classified.reason is reason
    assert classified.retryable is retryable
    assert classified.status_code == status
    assert (classified.provider, classified.model) == ("acme", "m")


def test_an_error_without_a_status_is_unknown_and_retried() -> None:
    classified = classify_api_error(openai.APIConnectionError(request=_REQUEST))

    assert classified.reason is FailoverReason.unknown
    assert classified.retryable is True
    assert classified.status_code is None


def test_a_status_is_found_on_the_cause_chain() -> None:
    try:
        try:
            raise _status_error(429)
        except openai.APIStatusError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        classified = classify_api_error(outer)

    assert classified.reason is FailoverReason.rate_limit


def test_a_rate_limit_error_without_a_status_counts_as_429() -> None:
    class RateLimitError(Exception):
        pass

    assert classify_api_error(RateLimitError("slow down")).reason is FailoverReason.rate_limit


def test_the_message_comes_from_the_body_first() -> None:
    body = {"error": {"message": "  model is busy  "}}

    assert classify_api_error(_status_error(503, body)).message == "model is busy"
    assert classify_api_error(RuntimeError("x" * 600)).message == "x" * 500


def test_rate_limits_and_overload_are_retried_even_as_client_errors() -> None:
    assert {FailoverReason.rate_limit, FailoverReason.overloaded} == RETRYABLE_CLIENT_REASONS
