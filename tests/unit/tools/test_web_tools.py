"""The web_search tool contract, independent of any real search backend."""

import asyncio
import json
from typing import ClassVar

import pytest

from mertina_agent.agent.core import Agent
from mertina_agent.agent.interrupt import InterruptSignal, bind_interrupt_signal
from mertina_agent.agent.transports import NormalizedResponse, ToolCall
from mertina_agent.agent.transports.types import Usage
from mertina_agent.agent.web_search_provider import WebSearchProvider
from mertina_agent.config import Settings
from mertina_agent.plugins.web.ddgs import DDGSWebSearchProvider
from mertina_agent.tools import web_tools
from mertina_agent.tools.model_tools import discover_builtin_tools, get_tool_definitions
from mertina_agent.tools.registry import ToolRegistry, registry
from mertina_agent.tools.web_tools import (
    WEB_SEARCH_SCHEMA,
    build_search_provider,
    configure_web_search,
    register_web_search,
    web_search_tool,
)


class FakeProvider(WebSearchProvider):
    """A search backend that records queries and returns canned hits."""

    def __init__(self, *, available=True):
        self.available = available
        self.queries = []

    @property
    def name(self):
        return "fake"

    def is_available(self):
        return self.available

    async def search(self, query, limit=5):
        self.queries.append((query, limit))
        hit = {"title": "Mertina", "url": "https://example.test", "description": "d", "position": 1}
        return {"success": True, "data": {"web": [hit]}}


def search(query="mertina", limit=5, provider=None):
    return asyncio.run(web_search_tool(query, limit, provider=provider or FakeProvider()))


def test_search_returns_the_provider_response_as_json():
    provider = FakeProvider()

    result = json.loads(search(provider=provider))

    assert result["success"] is True
    assert result["data"]["web"][0]["title"] == "Mertina"
    assert provider.queries == [("mertina", 5)]


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(3, 3), (0, 1), (500, 100), ("7", 7), ("many", 5), (None, 5), (True, 5), (2.9, 2)],
)
def test_limit_is_clamped_to_one_through_one_hundred(limit, expected):
    provider = FakeProvider()

    search(limit=limit, provider=provider)

    assert provider.queries[0][1] == expected


@pytest.mark.parametrize("query", ["", "   ", None, 42])
def test_blank_or_non_text_query_is_rejected(query):
    provider = FakeProvider()

    result = json.loads(search(query=query, provider=provider))

    assert result == {"error": "query must be a non-empty string", "success": False}
    assert provider.queries == []


def test_stopped_run_does_not_search():
    signal = InterruptSignal()
    signal.set()
    provider = FakeProvider()

    with bind_interrupt_signal(signal):
        result = json.loads(search(provider=provider))

    assert result == {"error": "Interrupted", "success": False}
    assert provider.queries == []


def test_registered_tool_is_offered_only_while_the_provider_is_available():
    tool_registry = ToolRegistry()
    provider = FakeProvider(available=False)
    register_web_search(provider, tool_registry=tool_registry)

    assert get_tool_definitions(tool_registry=tool_registry) == []
    provider.available = True
    assert get_tool_definitions(tool_registry=tool_registry) == [
        {"type": "function", "function": WEB_SEARCH_SCHEMA}
    ]


def test_registered_tool_dispatches_to_its_provider():
    tool_registry = ToolRegistry()
    provider = FakeProvider()
    register_web_search(provider, tool_registry=tool_registry)

    result = asyncio.run(tool_registry.dispatch("web_search", {"query": "q", "limit": 2}))

    assert json.loads(result)["success"] is True
    assert provider.queries == [("q", 2)]


def test_settings_select_the_ddgs_provider_and_its_timeout():
    provider = build_search_provider(Settings(web_search_timeout_s=7.5))

    assert isinstance(provider, DDGSWebSearchProvider)
    assert provider._timeout_s == 7.5


def test_configure_replaces_the_registered_provider():
    tool_registry = ToolRegistry()
    register_web_search(FakeProvider(), tool_registry=tool_registry)

    configure_web_search(Settings(), tool_registry=tool_registry)

    assert tool_registry.get_entry("web_search").toolset == "web"


def test_web_search_is_a_discovered_built_in_tool():
    assert discover_builtin_tools() == ["mertina_agent.tools.web_tools"]
    assert registry.get_entry("web_search") is not None
    assert web_tools.WEB_SEARCH_SCHEMA["name"] == "web_search"


def test_agent_feeds_wrapped_search_results_back_to_the_model():
    tool_registry = ToolRegistry()
    register_web_search(FakeProvider(), tool_registry=tool_registry)
    requests = []

    class Client:
        script: ClassVar[list[NormalizedResponse]] = [
            NormalizedResponse(
                content=None,
                tool_calls=(ToolCall(id="s1", name="web_search", arguments='{"query": "q"}'),),
                finish_reason="tool_calls",
                usage=Usage(1, 1, 2),
            ),
            NormalizedResponse(content="Found it.", tool_calls=(), finish_reason="stop"),
        ]

        async def complete(self, messages, *, tools=()):
            requests.append((list(messages), list(tools)))
            return self.script.pop(0)

        async def aclose(self):
            return None

    agent = Agent(Settings(), model_client=Client(), tool_registry=tool_registry)

    result = asyncio.run(agent.run_conversation("Search please"))

    tool_message = requests[1][0][-1]
    assert requests[0][1] == [{"type": "function", "function": WEB_SEARCH_SCHEMA}]
    assert tool_message["content"].startswith('<untrusted_tool_result source="web_search">')
    assert '"title": "Mertina"' in tool_message["content"]
    assert result["final_response"] == "Found it."
