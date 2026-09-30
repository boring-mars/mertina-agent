"""The retry loop's recovery helpers: backoff, the interruptible wait, attempt logging and the
terminal results."""

from typing import Any

import httpx
import openai
import pytest

from mertina.agent import retry_utils
from mertina.agent.error_classifier import classify_api_error
from mertina.agent.turn_recovery import (
    compute_error_backoff,
    interruptible_backoff_sleep,
    log_api_error_attempt,
    max_retries_exhausted_result,
    nonretryable_client_error_result,
    route_classified_error,
)

_REQUEST = httpx.Request("POST", "http://fake.test/v1/chat/completions")


def _status_error(
    status: int, headers: dict[str, str] | None = None, body: object = None
) -> openai.APIStatusError:
    response = httpx.Response(status, request=_REQUEST, headers=headers)
    return openai.APIStatusError("boom", response=response, body=body)


class _Agent:
    """Records what the recovery helpers show and log."""

    log_prefix = ""
    verbose_logging = False
    provider = "acme"
    base_url = "http://fake.test/v1"
    model = "m"

    def __init__(self) -> None:
        self._interrupt_requested = False
        self.emitted: list[str] = []
        self.buffered: list[str] = []
        self.waits: list[str] = []
        self.printed: list[str] = []
        self.flushes = 0

    def _emit_diagnostic_status(self, message: str) -> None:
        self.emitted.append(message)

    def _buffer_diagnostic_status(self, message: str) -> None:
        self.buffered.append(message)

    def _buffer_vprint(self, message: str) -> None:
        self.buffered.append(message)

    def _emit_diagnostic_wait(self, text: str) -> None:
        self.waits.append(text)

    def _vprint(self, *args: Any, **kwargs: Any) -> None:
        self.printed.append(args[0])

    def _flush_status_buffer(self) -> None:
        self.flushes += 1

    def _client_log_context(self) -> str:
        return "client"

    def _summarize_api_error(self, error: Exception) -> str:
        return f"summary of {error}"

    def clear_interrupt(self) -> bool:
        self._interrupt_requested = False
        return True


@pytest.fixture
def jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry_utils, "jittered_backoff", lambda *args, **kwargs: 1.5)


# --- compute_error_backoff ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "wait"),
    [
        (_status_error(429, headers={"retry-after": "7"}), 7.0),
        (_status_error(500, body={"error": {"retry_after": 3}}), 3.0),
        (_status_error(500, body={"retry_after": "4"}), 4.0),
        (_status_error(500, headers={"retry-after": "0"}), 1.5),
        (_status_error(500), 1.5),
    ],
)
@pytest.mark.usefixtures("jitter")
def test_retry_after_wins_over_the_jittered_backoff(error: Exception, wait: float) -> None:
    agent = _Agent()

    assert (
        compute_error_backoff(agent, error, retry_count=1, max_retries=3, is_rate_limited=False)
        == wait
    )
    assert agent.buffered == [f"⏳ Retrying in {wait:.1f}s (attempt 1/3)..."]
    assert agent.waits == [f"⏳ waiting on provider — retrying in {wait:.0f}s (attempt 1/3)"]


def test_a_long_retry_after_is_capped_and_shown_at_once() -> None:
    agent = _Agent()

    wait = compute_error_backoff(
        agent,
        _status_error(503, headers={"retry-after": "900"}),
        retry_count=1,
        max_retries=3,
        is_rate_limited=False,
    )

    assert wait == 600
    assert agent.emitted == ["⏳ Retrying in 600.0s (attempt 1/3)..."]
    assert agent.buffered == []


@pytest.mark.usefixtures("jitter")
def test_a_rate_limit_is_announced_as_one() -> None:
    agent = _Agent()

    compute_error_backoff(
        agent, _status_error(429), retry_count=2, max_retries=3, is_rate_limited=True
    )

    assert agent.buffered == ["⏱️ Rate limited. Waiting 1.5s (attempt 3/3)..."]


def test_only_rate_limits_are_marked_as_such() -> None:
    def marked(status: int) -> bool:
        error = _status_error(status)
        verdict = route_classified_error(
            _Agent(), error, classify_api_error(error), retry_count=1, max_retries=3
        )
        assert verdict.action == "fallthrough"
        return verdict.is_rate_limited

    assert marked(429) is True
    assert marked(503) is False


# --- the interruptible wait and attempt logging ---------------------------------------------------


def test_the_wait_ends_early_on_an_interrupt() -> None:
    agent = _Agent()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]

    assert (
        interruptible_backoff_sleep(
            agent, 0.0, messages=messages, api_call_count=1, abort_message="a", interrupt_text="t"
        )
        is None
    )

    agent._interrupt_requested = True
    result = interruptible_backoff_sleep(
        agent,
        30.0,
        messages=messages,
        api_call_count=1,
        abort_message="stopping",
        interrupt_text="Stopped.",
    )

    assert result == {
        "final_response": "Stopped.",
        "messages": messages,
        "api_calls": 1,
        "completed": False,
        "interrupted": True,
    }
    assert agent._interrupt_requested is False
    assert agent.printed == ["⚡ stopping"]


def test_an_attempt_is_logged_and_buffered() -> None:
    agent = _Agent()
    error = _status_error(401)

    logged = log_api_error_attempt(
        agent,
        error,
        retry_count=1,
        max_retries=3,
        status_code=401,
        elapsed_time=0.5,
        api_messages=[],
        retryable=False,
    )

    assert logged == ("APIStatusError", "boom", "acme", "http://fake.test/v1", "m")
    assert agent.buffered == ["⚠️  Attempt 1/3, not retryable failed: summary of boom"]


# --- terminal results -----------------------------------------------------------------------------


def test_a_non_retryable_error_ends_the_turn() -> None:
    agent = _Agent()
    error = _status_error(400)
    messages: list[dict[str, Any]] = []

    result = nonretryable_client_error_result(
        agent,
        error,
        classify_api_error(error),
        status_code=400,
        messages=messages,
        api_call_count=1,
        provider="acme",
        base_url="",
        model="m",
    )

    assert agent.flushes == 1
    assert agent.emitted == [
        "❌ acme rejected the request and retrying won't help: summary of boom"
    ]
    assert result["failed"] is True
    assert result["failure_reason"] == "format_error"
    assert result["failure_retryable"] is False
    assert result["error"] == "summary of boom"
    assert result["final_response"].startswith("acme rejected this request as malformed")


@pytest.mark.parametrize(
    ("is_rate_limited", "status"),
    [
        (True, "❌ Rate limited after 3 retries — summary of boom"),
        (False, "❌ API failed after 3 retries — summary of boom"),
    ],
)
def test_exhausted_retries_end_the_turn(is_rate_limited: bool, status: str) -> None:
    agent = _Agent()
    error = _status_error(429 if is_rate_limited else 500)

    result = max_retries_exhausted_result(
        agent,
        error,
        classify_api_error(error),
        max_retries=3,
        is_rate_limited=is_rate_limited,
        api_messages=[],
        messages=[],
        api_call_count=3,
        provider="acme",
        base_url="",
        model="m",
    )

    assert agent.emitted == [status]
    assert result["failed"] is True
    assert result["failure_retryable"] is True
    assert result["final_response"].endswith("\n\nProvider said: summary of boom")
