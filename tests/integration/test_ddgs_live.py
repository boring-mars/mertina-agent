"""Live DuckDuckGo search through the real worker process; no model is called."""

import asyncio

import pytest

from mertina_agent.plugins.web.ddgs import DDGSWebSearchProvider

pytestmark = pytest.mark.integration


def test_live_search_returns_normalized_hits():
    result = asyncio.run(DDGSWebSearchProvider().search("Python programming language", 3))

    if result["success"] is False:
        pytest.skip(f"DuckDuckGo unavailable or rate-limiting: {result['error']}")
    hits = result["data"]["web"]
    assert 1 <= len(hits) <= 3
    assert [hit["position"] for hit in hits] == list(range(1, len(hits) + 1))
    assert all(hit["url"].startswith("http") for hit in hits)
