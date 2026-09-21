"""Tool-call validation branches for batches mixing valid and broken calls."""

import asyncio

import pytest

from mertina_agent.agent.core import Agent


@pytest.fixture
def run_script(settings, tool_registry, fake):
    def _run(script):
        client = fake.client(script)
        agent = Agent(settings, model_client=client, tool_registry=tool_registry)
        return asyncio.run(agent.run_conversation("Hi")), client

    return _run


def test_broken_arguments_of_an_unknown_call_do_not_retry_the_batch(
    run_script, fake, tool_calls_seen
):
    batch = fake.calls(fake.call("bad", name="missing", arguments='{"x": }'), fake.call("good"))

    result, client = run_script([batch, fake.text()])

    assert len(client.requests) == 2
    assert tool_calls_seen == [{}]
    fake.assert_replayable(result["messages"])


def test_injected_json_errors_skip_the_sibling_calls(
    run_script, fake, tool_registry, tool_calls_seen
):
    tool_registry.register("other", "core", {"name": "other"}, lambda _args: "other ran")
    broken = [
        fake.calls(
            fake.call(f"a{index}", name="echo", arguments='{"x": }'),
            fake.call(f"b{index}", name="other"),
        )
        for index in range(3)
    ]

    result, client = run_script([*broken, fake.text()])

    injected = client.requests[3]["messages"][-2:]
    assert injected[0]["content"].startswith("Error: Invalid JSON arguments.")
    assert injected[1]["content"] == "Skipped: other tool call in this response had invalid JSON."
    assert tool_calls_seen == []
    fake.assert_replayable(result["messages"])
