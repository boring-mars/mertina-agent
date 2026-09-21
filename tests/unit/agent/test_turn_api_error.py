"""Model-call retries and their terminal outcomes (Hermes retry-loop intent)."""

import asyncio

import pytest

from mertina_agent.agent.core import Agent
from mertina_agent.agent.events import RetryScheduled
from mertina_agent.agent.interrupt import InterruptSignal, bind_interrupt_signal
from mertina_agent.agent.turn_api_error import interruptible_backoff_sleep
from mertina_agent.agent.turn_failure_copy import (
    FAILED_TURN_NOTICE,
    RATE_LIMITED_EXHAUSTED,
    RETRIES_EXHAUSTED,
)
from mertina_agent.config import Settings
from mertina_agent.exceptions import ModelRequestError, ModelResponseError


@pytest.fixture
def make_agent(settings, tool_registry, fake):
    def _make(script, *, max_attempts=3, sleep=None, events=None, settings_override=None):
        sleep = sleep or fake.sleep()
        client = fake.client(script)
        agent = Agent(
            settings_override or settings,
            model_client=client,
            tool_registry=tool_registry,
            retry_policy=fake.retry_policy(max_attempts, sleep=sleep),
            event_callback=events.append if events is not None else None,
        )
        return agent, client, sleep

    return _make


def run(agent):
    return asyncio.run(agent.run_conversation("Hi"))


def http(status, retry_after=None):
    return ModelRequestError(
        f"Model endpoint returned HTTP {status}",
        kind="http",
        status_code=status,
        retry_after=retry_after,
    )


@pytest.mark.parametrize(
    "error",
    [
        ModelRequestError("Model request timed out", kind="timeout"),
        ModelRequestError("Could not connect to the model endpoint", kind="connection"),
        http(408),
        http(429),
        http(500),
        http(503),
        ModelResponseError("Model endpoint returned an invalid response"),
    ],
    ids=["timeout", "connection", "408", "429", "500", "503", "invalid-response"],
)
def test_transient_failures_are_retried(make_agent, fake, error):
    agent, client, sleep = make_agent([error, fake.text("Recovered.")])

    result = run(agent)

    assert result["final_response"] == "Recovered."
    assert result["completed"] is True
    assert len(sleep.waits) == 1
    assert client.requests[0]["messages"] == client.requests[1]["messages"]


def test_retries_do_not_count_as_iterations(make_agent, fake):
    agent, _, _ = make_agent(
        [http(503), http(503), fake.text("ok")],
        settings_override=Settings(llm_model="m", max_iterations=1),
    )

    result = run(agent)

    assert result["api_calls"] == 1
    assert result["completed"] is True


@pytest.mark.parametrize(
    "error",
    [http(400), http(401), http(404), ModelRequestError("Model client is closed", kind="closed")],
    ids=["400", "401", "404", "closed"],
)
def test_permanent_failures_end_the_turn_without_retrying(make_agent, error):
    agent, client, sleep = make_agent([error])

    result = run(agent)

    assert result["failed"] is True
    assert result["turn_exit_reason"] == "model_request_failed"
    assert sleep.waits == []
    assert len(client.requests) == 1
    assert result["messages"][-1]["content"] == FAILED_TURN_NOTICE


def test_exhausted_attempts_end_the_turn(make_agent):
    agent, client, sleep = make_agent([http(503)] * 3)

    result = run(agent)

    assert len(client.requests) == 3
    assert len(sleep.waits) == 2
    assert result["failed"] is True
    assert result["turn_exit_reason"] == "all_retries_exhausted"
    assert result["final_response"] == RETRIES_EXHAUSTED.format(
        attempts=3, detail="Model endpoint returned HTTP 503"
    )


def test_exhausted_rate_limit_explains_the_wait(make_agent):
    agent, _, _ = make_agent([http(429)] * 2, max_attempts=2)

    result = run(agent)

    assert result["final_response"] == RATE_LIMITED_EXHAUSTED.format(
        attempts=2, detail="Model endpoint returned HTTP 429"
    )


def test_single_attempt_policy_never_retries(make_agent):
    agent, client, sleep = make_agent([http(503)], max_attempts=1)

    result = run(agent)

    assert len(client.requests) == 1
    assert sleep.waits == []
    assert result["turn_exit_reason"] == "all_retries_exhausted"


@pytest.mark.parametrize(
    ("retry_after", "expected_wait"), [("7", 7.0), ("9999", 600.0)], ids=["honoured", "capped"]
)
def test_retry_after_sets_the_wait(make_agent, fake, retry_after, expected_wait):
    agent, _, sleep = make_agent([http(429, retry_after), fake.text()])

    run(agent)

    assert sleep.waits == [expected_wait]


def test_zero_retry_after_falls_back_to_jittered_backoff(make_agent, fake):
    agent, _, sleep = make_agent([http(503, "0"), fake.text()])

    run(agent)

    assert 2.0 <= sleep.waits[0] <= 3.0


def test_malformed_responses_back_off_longer(make_agent, fake):
    agent, _, sleep = make_agent([ModelResponseError("invalid"), fake.text()])

    run(agent)

    assert 5.0 <= sleep.waits[0] <= 7.5


def test_retry_is_reported_as_an_event(make_agent, fake):
    events = []
    agent, _, _ = make_agent([http(503), fake.text()], events=events)

    run(agent)

    retry = next(event for event in events if isinstance(event, RetryScheduled))
    assert (retry.attempt, retry.max_attempts, retry.reason) == (1, 3, "http 503")


def test_stop_during_backoff_ends_the_turn_as_interrupted(make_agent, fake):
    agent, client, _ = make_agent([http(503), fake.text()], sleep=fake.sleep(stop_on_call=1))

    result = run(agent)

    assert len(client.requests) == 1
    assert result["interrupted"] is True
    assert result["turn_exit_reason"] == "interrupted_during_retry"
    assert result["final_response"] == (
        "Operation interrupted: retrying API call after error (retry 1/3)."
    )
    fake.assert_replayable(result["messages"])


def test_stop_requested_while_the_failing_call_ran_skips_the_retry(make_agent, fake):
    agent, client, sleep = make_agent([])

    async def fail_after_stop(_messages, *, tools=()):
        del tools
        agent.interrupt("user_stop")
        message = "Model request timed out"
        raise ModelRequestError(message, kind="timeout")

    client.complete = fail_after_stop

    result = run(agent)

    assert sleep.waits == []
    assert result["final_response"] == "Operation interrupted: handling API error (timeout)."
    fake.assert_replayable(result["messages"])


def test_default_backoff_sleeps_when_no_run_is_bound():
    assert asyncio.run(interruptible_backoff_sleep(0.01)) is False


def test_default_backoff_returns_early_when_the_run_is_stopped():
    signal = InterruptSignal()

    async def scenario():
        with bind_interrupt_signal(signal):
            asyncio.get_running_loop().call_later(0.01, signal.set)
            return await interruptible_backoff_sleep(30)

    assert asyncio.run(asyncio.wait_for(scenario(), 5)) is True


def test_failed_stream_is_retried_after_telling_consumers_to_discard_partial_text(
    settings, tool_registry, fake
):
    class FlakyStream:
        def __init__(self):
            self.attempts = 0

        async def stream(self, messages, *, tools=(), on_text_delta=None, on_tool_started=None):
            del messages, tools, on_tool_started
            self.attempts += 1
            if self.attempts == 1:
                on_text_delta("Partial ans")
                message = "Model stream ended before the response was complete."
                raise ModelResponseError(message)
            on_text_delta("Full answer.")
            return fake.text("Full answer.")

        async def aclose(self):
            return None

    events = []
    agent = Agent(
        settings,
        model_client=FlakyStream(),
        tool_registry=tool_registry,
        retry_policy=fake.retry_policy(),
        event_callback=events.append,
    )

    result = run(agent)

    assert [type(event).__name__ for event in events] == [
        "TextDelta",
        "RetryScheduled",
        "TextDelta",
        "RunCompleted",
    ]
    assert result["final_response"] == "Full answer."
