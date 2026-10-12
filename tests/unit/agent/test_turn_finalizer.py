"""Budget-exhaustion summary edge cases (Hermes iteration-limit exit intent)."""

import asyncio

import pytest

from mertina_agent.agent.core import Agent
from mertina_agent.agent.turn_failure_copy import EMPTY_SUMMARY_RESPONSE
from mertina_agent.config import Settings


@pytest.fixture
def one_iteration_agent(tool_registry, fake):
    def _make(summary_script):
        client = fake.client([fake.calls(fake.call()), *summary_script])
        settings = Settings(llm_model="m", max_iterations=1)
        return Agent(settings, model_client=client, tool_registry=tool_registry), client

    return _make


def test_summary_tool_calls_are_discarded_and_its_text_kept(one_iteration_agent, fake):
    agent, _ = one_iteration_agent([fake.calls(fake.call("s"), content="Summary text.")])

    result = asyncio.run(agent.run_conversation("Hi"))

    assert result["final_response"] == "Summary text."
    assert result["messages"][-1] == {"role": "assistant", "content": "Summary text."}


def test_summary_without_text_is_retried_once(one_iteration_agent, fake):
    agent, client = one_iteration_agent([fake.calls(fake.call("s")), fake.text("Second try.")])

    result = asyncio.run(agent.run_conversation("Hi"))

    assert result["final_response"] == "Second try."
    assert result["api_calls"] == 3
    assert client.requests[1]["messages"] == client.requests[2]["messages"]


def test_summary_think_block_is_stripped(one_iteration_agent, fake):
    agent, _ = one_iteration_agent([fake.text("<think>plan</think>\nThe summary.")])

    assert asyncio.run(agent.run_conversation("Hi"))["final_response"] == "The summary."


def test_think_only_summary_falls_back_to_a_fixed_answer(one_iteration_agent, fake):
    agent, _ = one_iteration_agent([fake.text("<think>only thoughts</think>")])

    result = asyncio.run(agent.run_conversation("Hi"))

    assert result["final_response"] == EMPTY_SUMMARY_RESPONSE
    fake.assert_replayable(result["messages"])


def test_two_textless_summaries_fall_back_to_a_fixed_answer(one_iteration_agent, fake):
    agent, _ = one_iteration_agent([fake.calls(fake.call("s1")), fake.calls(fake.call("s2"))])

    result = asyncio.run(agent.run_conversation("Hi"))

    assert result["final_response"] == EMPTY_SUMMARY_RESPONSE
    assert result["api_calls"] == 3
    fake.assert_replayable(result["messages"])
