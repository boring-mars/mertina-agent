"""Agent ReAct turns with a scripted model (Hermes test_run_agent intent)."""

import asyncio
import copy
import json

import pytest

from mertina_agent.agent.core import Agent
from mertina_agent.agent.events import RunCompleted, ToolCallFinished, ToolCallStarted
from mertina_agent.agent.transports import NormalizedResponse
from mertina_agent.agent.transports.types import Usage
from mertina_agent.agent.turn_failure_copy import (
    FAILED_TURN_NOTICE,
    MAX_ITERATIONS_NO_SUMMARY,
    PARTIAL_FAILED_TURN_NOTICE,
    TRUNCATED_RESPONSE,
)
from mertina_agent.agent.turn_finalizer import MAX_ITERATIONS_SUMMARY_REQUEST
from mertina_agent.config import Settings
from mertina_agent.exceptions import (
    AgentBusyError,
    ConfigurationError,
    ModelInputError,
    ModelRequestError,
)


@pytest.fixture
def make_agent(settings, tool_registry, fake):
    def _make(script, **kwargs):
        client = fake.client(script)
        options = {
            "tool_registry": tool_registry,
            "clock": lambda: fake.now,
            "retry_policy": fake.retry_policy(),
            **kwargs,
        }
        return Agent(options.pop("settings", settings), model_client=client, **options), client

    return _make


def run(agent, message="Hi", **kwargs):
    return asyncio.run(agent.run_conversation(message, **kwargs))


def test_plain_answer_completes_in_one_call(make_agent, fake):
    agent, client = make_agent([fake.text("Hello!")])

    result = run(agent)

    assert result["final_response"] == "Hello!"
    assert result["completed"] is True
    assert result["api_calls"] == 1
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert [message["role"] for message in result["messages"]] == ["user", "assistant"]
    assert client.requests[0]["tools"] == agent.tools


def test_chat_returns_only_the_final_text(make_agent, fake):
    agent, _ = make_agent([fake.text("Hello!")])

    assert asyncio.run(agent.chat("Hi")) == "Hello!"


def test_request_starts_with_the_system_prompt(make_agent, fake):
    agent, client = make_agent([fake.text()], system_message="Answer in French.")

    run(agent)

    system = client.requests[0]["messages"][0]
    assert system["role"] == "system"
    assert "You are Mertina Agent." in system["content"]
    assert "Answer in French." in system["content"]
    assert "Conversation started: Monday, September 21, 2026" in system["content"]


def test_system_prompt_is_byte_stable_across_turns(make_agent, fake):
    times = iter([fake.now, fake.now.replace(year=2030)])
    agent, client = make_agent([fake.text(), fake.text()], clock=lambda: next(times))

    first = run(agent)
    run(agent, "Again", conversation_history=first["messages"])

    assert client.requests[0]["messages"][0] == client.requests[1]["messages"][0]


def test_single_tool_round_trip_feeds_the_result_back(make_agent, fake, tool_calls_seen):
    agent, client = make_agent(
        [fake.calls(fake.call("c1", arguments='{"text": "ping"}')), fake.text("pong")]
    )

    result = run(agent)

    assert tool_calls_seen == [{"text": "ping"}]
    second_request = client.requests[1]["messages"]
    assert second_request[-2]["tool_calls"][0]["id"] == "c1"
    assert second_request[-1] == {
        "role": "tool",
        "content": json.dumps({"echo": {"text": "ping"}}),
        "tool_call_id": "c1",
    }
    assert result["final_response"] == "pong"
    assert result["api_calls"] == 2
    fake.assert_replayable(result["messages"])


def test_multiple_tool_calls_are_answered_in_call_order(make_agent, fake):
    batch = fake.calls(fake.call("a", arguments='{"text": "1"}'), fake.call("b", arguments="{}"))
    agent, _ = make_agent([batch, fake.text()])

    messages = run(agent)["messages"]

    assert [message.get("tool_call_id") for message in messages[2:4]] == ["a", "b"]


def test_events_report_tool_progress_and_completion(make_agent, fake):
    events = []
    agent, _ = make_agent([fake.calls(fake.call("c1")), fake.text()], event_callback=events.append)

    run(agent)

    assert [type(event) for event in events] == [ToolCallStarted, ToolCallFinished, RunCompleted]
    assert events[0].call_id == "c1"
    assert events[1].is_error is False
    assert events[2].completed is True


def test_failing_event_callback_does_not_break_the_turn(make_agent, fake):
    def broken(_event):
        message = "consumer bug"
        raise RuntimeError(message)

    agent, _ = make_agent([fake.calls(fake.call()), fake.text("ok")], event_callback=broken)

    assert run(agent)["final_response"] == "ok"


def test_history_is_continued_without_modifying_the_callers_copy(make_agent, fake):
    agent, client = make_agent([fake.calls(fake.call()), fake.text("one"), fake.text("two")])
    first = run(agent)
    history = copy.deepcopy(first["messages"])

    second = run(agent, "Next", conversation_history=first["messages"])

    assert first["messages"] == history
    assert client.requests[2]["messages"][1:] == [*history, {"role": "user", "content": "Next"}]
    assert second["messages"][: len(history)] == history


def test_agent_without_tools_sends_no_tools_or_tool_guidance(settings, fake):
    client = fake.client([fake.text()])
    agent = Agent(settings, model_client=client, enabled_toolsets=[], clock=lambda: fake.now)

    run(agent)

    assert client.requests[0]["tools"] == []
    assert "# Parallel tool calls" not in client.requests[0]["messages"][0]["content"]


def test_unknown_toolset_fails_at_construction(settings, tool_registry, fake):
    with pytest.raises(ConfigurationError):
        Agent(
            settings,
            model_client=fake.client([]),
            tool_registry=tool_registry,
            enabled_toolsets=["missing"],
        )


def test_budget_exhaustion_requests_a_toolless_summary(make_agent, fake):
    looping = [fake.calls(fake.call(f"c{index}")) for index in range(2)]
    agent, client = make_agent(
        [*looping, fake.text("Summary.")], settings=Settings(llm_model="m", max_iterations=2)
    )

    result = run(agent)

    assert result["final_response"] == "Summary."
    assert result["completed"] is False
    assert result["turn_exit_reason"] == "max_iterations_reached(2/2)"
    assert result["api_calls"] == 3
    assert client.requests[2]["tools"] == []
    assert client.requests[2]["messages"][-1]["content"] == MAX_ITERATIONS_SUMMARY_REQUEST
    fake.assert_replayable(result["messages"])


def test_failed_summary_still_closes_the_turn(make_agent, fake):
    agent, _ = make_agent(
        [fake.calls(fake.call()), ModelRequestError("down", kind="connection")],
        settings=Settings(llm_model="m", max_iterations=1),
    )

    result = run(agent)

    assert result["final_response"] == MAX_ITERATIONS_NO_SUMMARY.format(limit=1)
    fake.assert_replayable(result["messages"])


def test_unknown_tool_is_reported_so_the_model_can_correct_itself(
    make_agent, fake, tool_calls_seen
):
    agent, client = make_agent(
        [fake.calls(fake.call("x", name="missing")), fake.calls(fake.call("y")), fake.text()]
    )

    result = run(agent)

    error = client.requests[1]["messages"][-1]
    assert error["tool_call_id"] == "x"
    assert error["content"] == "Tool 'missing' does not exist. Available tools: echo"
    assert tool_calls_seen == [{}]
    assert result["completed"] is True


def test_three_all_invalid_batches_end_the_turn_as_partial(make_agent, fake):
    invalid = [fake.calls(fake.call(f"x{index}", name="missing")) for index in range(3)]
    agent, _ = make_agent(invalid)

    result = run(agent)

    assert result["partial"] is True
    assert result["final_response"] == "Model generated invalid tool call: missing"
    fake.assert_replayable(result["messages"])


def test_mixed_batch_runs_valid_calls_and_errors_unknown_ones(make_agent, fake, tool_calls_seen):
    batch = fake.calls(fake.call("bad", name="missing"), fake.call("good"))
    agent, _ = make_agent([batch, fake.text()])

    messages = run(agent)["messages"]

    assert tool_calls_seen == [{}]
    assert [message["tool_call_id"] for message in messages[2:4]] == ["bad", "good"]
    assert "does not exist" in messages[2]["content"]


def test_invalid_json_arguments_retry_the_request_without_recording_it(
    make_agent, fake, tool_calls_seen
):
    agent, client = make_agent(
        [fake.calls(fake.call(arguments='{"text": }')), fake.calls(fake.call()), fake.text()]
    )

    result = run(agent)

    assert client.requests[1]["messages"] == client.requests[0]["messages"]
    assert tool_calls_seen == [{}]
    assert result["api_calls"] == 3


def test_repeated_invalid_json_injects_error_results(make_agent, fake, tool_calls_seen):
    broken = [fake.calls(fake.call(f"c{index}", arguments='{"text": }')) for index in range(3)]
    agent, client = make_agent([*broken, fake.text()])

    result = run(agent)

    injected = client.requests[3]["messages"][-1]
    assert injected["tool_call_id"] == "c2"
    assert injected["content"].startswith("Error: Invalid JSON arguments.")
    assert tool_calls_seen == []
    fake.assert_replayable(result["messages"])


def test_truncated_tool_arguments_are_refused(make_agent, fake, tool_calls_seen):
    agent, _ = make_agent([fake.calls(fake.call(arguments='{"text": "unfinish'))])

    result = run(agent)

    assert tool_calls_seen == []
    assert result["final_response"] == TRUNCATED_RESPONSE
    assert result["partial"] is True
    fake.assert_replayable(result["messages"])


def test_empty_arguments_are_recorded_as_an_empty_object(make_agent, fake, tool_calls_seen):
    agent, _ = make_agent([fake.calls(fake.call(arguments="")), fake.text()])

    messages = run(agent)["messages"]

    assert messages[1]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert tool_calls_seen == [{}]


def test_tool_calls_cut_off_by_the_length_limit_are_not_executed(make_agent, fake, tool_calls_seen):
    agent, _ = make_agent([fake.calls(fake.call(), finish_reason="length")])

    result = run(agent)

    assert tool_calls_seen == []
    assert result["final_response"] == TRUNCATED_RESPONSE
    assert result["turn_exit_reason"] == "length_truncated"
    fake.assert_replayable(result["messages"])


def test_text_cut_off_by_the_length_limit_is_returned_as_partial(make_agent, fake):
    agent, _ = make_agent([fake.text("Half an ans", finish_reason="length")])

    result = run(agent)

    assert result["final_response"] == "Half an ans"
    assert result["partial"] is True
    assert result["messages"][-1] == {"role": "assistant", "content": "Half an ans"}


def test_content_filter_ends_the_turn_as_failed(make_agent, fake):
    agent, _ = make_agent([fake.text(None, finish_reason="content_filter", refusal="Not allowed.")])

    result = run(agent)

    assert result["failed"] is True
    assert result["final_response"].startswith("⚠️")
    assert "Provider said: Not allowed." in result["final_response"]
    assert result["messages"][-1]["content"] == FAILED_TURN_NOTICE


def test_refusal_is_returned_as_the_answer_and_kept_in_history(make_agent, fake):
    agent, _ = make_agent([fake.text(None, refusal="I can't help with that.")])

    result = run(agent)

    assert result["final_response"] == "I can't help with that."
    assert result["messages"][-1]["refusal"] == "I can't help with that."
    fake.assert_replayable(result["messages"])


def test_model_request_failure_ends_the_turn_with_a_valid_history(make_agent, fake):
    agent, _ = make_agent(
        [ModelRequestError("Model endpoint returned HTTP 401", kind="http", status_code=401)]
    )

    result = run(agent)

    assert result["failed"] is True
    assert result["error"] == "Model endpoint returned HTTP 401"
    assert result["messages"][-1]["content"] == FAILED_TURN_NOTICE
    fake.assert_replayable(result["messages"])


def test_failure_after_a_tool_ran_warns_that_actions_may_have_run(make_agent, fake):
    agent, _ = make_agent(
        [fake.calls(fake.call()), ModelRequestError("HTTP 403", kind="http", status_code=403)]
    )

    result = run(agent)

    assert result["messages"][-1]["content"] == PARTIAL_FAILED_TURN_NOTICE
    fake.assert_replayable(result["messages"])


def test_usage_is_summed_over_the_turn(make_agent, fake):
    agent, _ = make_agent(
        [fake.calls(fake.call(), usage=Usage(10, 2, 12)), fake.text(usage=Usage(20, 3, 23))]
    )

    assert run(agent)["usage"] == Usage(30, 5, 35)


def test_unreported_usage_makes_the_total_unknown(make_agent, fake):
    agent, _ = make_agent(
        [fake.calls(fake.call(), usage=Usage(10, 2, 12)), fake.text(usage=Usage())]
    )

    assert run(agent)["usage"] == Usage()


def test_a_response_without_usage_makes_the_total_unknown(make_agent):
    silent = NormalizedResponse(content="ok", tool_calls=(), finish_reason="stop", usage=None)
    agent, _ = make_agent([silent])

    assert run(agent)["usage"] == Usage()


def test_lone_surrogates_in_the_user_message_are_replaced(make_agent, fake):
    agent, client = make_agent([fake.text()])

    run(agent, "bad \ud800 text")

    assert client.requests[0]["messages"][-1]["content"] == "bad � text"


def test_non_text_user_message_is_rejected(make_agent):
    agent, _ = make_agent([])

    with pytest.raises(ModelInputError):
        run(agent, 42)


def test_a_second_turn_cannot_start_while_one_is_running(settings, tool_registry, fake):
    release = asyncio.Event()

    class SlowClient(fake.client):
        async def complete(self, messages, *, tools=()):
            await release.wait()
            return await super().complete(messages, tools=tools)

    agent = Agent(settings, model_client=SlowClient([fake.text()]), tool_registry=tool_registry)

    async def scenario():
        first = asyncio.create_task(agent.run_conversation("one"))
        await asyncio.sleep(0)
        with pytest.raises(AgentBusyError):
            await agent.run_conversation("two")
        release.set()
        return await first

    assert asyncio.run(scenario())["completed"] is True


def test_injected_client_is_left_open_on_close(make_agent):
    agent, client = make_agent([])

    asyncio.run(agent.aclose())
    asyncio.run(agent.aclose())

    assert client.closed is False


def test_owned_client_is_closed_when_the_context_exits(settings, tool_registry):
    async def scenario():
        async with Agent(settings, tool_registry=tool_registry) as agent:
            pass
        return agent._model_client

    client = asyncio.run(scenario())

    with pytest.raises(ModelRequestError):
        asyncio.run(client.complete([{"role": "user", "content": "x"}]))
