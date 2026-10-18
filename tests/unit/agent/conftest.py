"""Fakes at the model boundary shared by the agent-loop tests."""

import copy
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from mertina_agent.agent.transports import ChatCompletionsTransport, NormalizedResponse, ToolCall
from mertina_agent.agent.transports.types import Usage
from mertina_agent.agent.turn_api_error import RetryPolicy
from mertina_agent.config import Settings
from mertina_agent.tools.registry import ToolRegistry

FIXED_NOW = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)


@dataclass
class ScriptedModelClient:
    """Return scripted responses in order and record every request it receives."""

    script: list[NormalizedResponse | Exception]
    requests: list[dict] = field(default_factory=list)
    closed: bool = False
    streamed_calls: int = 0

    async def complete(self, messages, *, tools=()):
        self.requests.append({"messages": copy.deepcopy(list(messages)), "tools": list(tools)})
        if not self.script:
            message = "The model was called more times than scripted"
            raise AssertionError(message)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def stream(self, messages, *, tools=(), on_text_delta=None, on_tool_started=None):
        """Deliver the scripted response the way ``ModelClient.stream`` reports it."""
        self.streamed_calls += 1
        response = await self.complete(messages, tools=tools)
        for tool_call in response.tool_calls:
            if on_tool_started is not None:
                on_tool_started(tool_call.name)
        if response.content and not response.tool_calls and on_text_delta is not None:
            on_text_delta(response.content)
        return response

    async def aclose(self):
        self.closed = True


def text(content="Done.", *, finish_reason="stop", usage=None, refusal=None):
    return NormalizedResponse(
        content=content,
        tool_calls=(),
        finish_reason=finish_reason,
        refusal=refusal,
        usage=usage if usage is not None else Usage(1, 1, 2),
    )


def calls(*tool_calls, content=None, finish_reason="tool_calls", usage=None):
    return NormalizedResponse(
        content=content,
        tool_calls=tuple(tool_calls),
        finish_reason=finish_reason,
        usage=usage if usage is not None else Usage(1, 1, 2),
    )


def call(identifier="call_1", name="echo", arguments="{}"):
    return ToolCall(id=identifier, name=name, arguments=arguments)


def assert_replayable(messages):
    """The history must be accepted by the real transport and pair every tool call."""
    ChatCompletionsTransport().convert_messages(messages)
    for index, message in enumerate(messages):
        if message["role"] == "assistant" and message.get("tool_calls"):
            expected = [item["id"] for item in message["tool_calls"]]
            following = messages[index + 1 : index + 1 + len(expected)]
            assert [item.get("tool_call_id") for item in following] == expected
    assert messages[-1]["role"] == "assistant"


@pytest.fixture
def settings():
    return Settings(llm_model="test-model", max_iterations=5)


@pytest.fixture
def tool_calls_seen():
    return []


@pytest.fixture
def tool_registry(tool_calls_seen):
    tool_registry = ToolRegistry()

    def echo(args):
        tool_calls_seen.append(args)
        return json.dumps({"echo": args})

    tool_registry.register(
        "echo",
        "core",
        {
            "name": "echo",
            "description": "Echo the arguments back.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
        echo,
    )
    return tool_registry


@dataclass
class RecordingSleep:
    """Backoff that returns at once, records each wait and can report a stop."""

    waits: list[float] = field(default_factory=list)
    stop_on_call: int | None = None

    async def __call__(self, wait_s):
        self.waits.append(wait_s)
        return self.stop_on_call is not None and len(self.waits) >= self.stop_on_call


def retry_policy(max_attempts=3, *, sleep=None, seed=7):
    return RetryPolicy(
        max_attempts=max_attempts, rng=random.Random(seed), sleep=sleep or RecordingSleep()
    )


@pytest.fixture
def fake():
    """Helpers for scripting model responses, exposed as a fixture for test modules."""
    return SimpleNamespace(
        client=ScriptedModelClient,
        text=text,
        calls=calls,
        call=call,
        assert_replayable=assert_replayable,
        now=FIXED_NOW,
        retry_policy=retry_policy,
        sleep=RecordingSleep,
    )
