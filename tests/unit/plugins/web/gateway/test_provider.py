"""Gateway web-search provider tests at the HTTP boundary."""

import asyncio
import json

from mertina_agent.plugins.web.gateway import GatewayWebSearchProvider


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self._payload


def test_provider_parses_citations_and_deduplicates_urls(monkeypatch):
    payload = {
        "output": [{
            "type": "message",
            "content": [{
                "text": "result text",
                "annotations": [
                    {"type": "url_citation", "title": "One", "url": "https://one.test"},
                    {"type": "url_citation", "title": "Again", "url": "https://one.test"},
                ],
            }],
        }]
    }
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data)
        return FakeResponse(payload)

    monkeypatch.setattr("mertina_agent.plugins.web.gateway.provider.request.urlopen", fake_urlopen)
    provider = GatewayWebSearchProvider(
        "https://gateway.test/v1", "secret", "search-model", timeout_s=7
    )

    result = asyncio.run(provider.search("query", 1))

    assert result == {
        "success": True,
        "data": {"web": [{
            "title": "One",
            "url": "https://one.test",
            "description": "result text",
            "position": 1,
        }]},
    }
    assert captured == {
        "url": "https://gateway.test/v1/responses",
        "timeout": 7,
        "body": {
            "model": "search-model",
            "tools": [{"type": "web_search"}],
            "input": "Search the web for: query. Return up to 1 relevant results.",
        },
    }


def test_provider_is_available_only_with_complete_configuration():
    provider = GatewayWebSearchProvider("https://gateway.test/v1", "key", "model")
    assert provider.name == "gateway"
    assert provider.is_available()
    assert GatewayWebSearchProvider("https://gateway.test/v1", "", "model").is_available()
    assert not GatewayWebSearchProvider("", "key", "model").is_available()


def test_provider_handles_unconfigured_and_sparse_gateway_output(monkeypatch):
    unavailable = GatewayWebSearchProvider("", "", "model")
    assert asyncio.run(unavailable.search("query")) == {
        "success": False,
        "error": "Gateway web search is not configured",
    }

    payload = {
        "output": [
            {"type": "web_search_call"},
            {
                "type": "message",
                "content": [{
                    "text": "See https://one.test and https://two.test.",
                    "annotations": [
                        {"type": "other", "url": "https://ignored.test"},
                        {"type": "url_citation"},
                    ],
                }],
            },
        ]
    }
    captured = {}

    def fake_urlopen(req, **_kwargs):
        captured["authorization"] = req.headers.get("Authorization")
        return FakeResponse(payload)

    monkeypatch.setattr("mertina_agent.plugins.web.gateway.provider.request.urlopen", fake_urlopen)
    provider = GatewayWebSearchProvider("https://gateway.test/v1", "", "model")

    result = asyncio.run(provider.search("query"))

    assert captured["authorization"] is None
    assert [hit["url"] for hit in result["data"]["web"]] == [
        "https://one.test",
        "https://two.test",
    ]


def test_provider_skips_text_url_already_returned_as_a_citation(monkeypatch):
    payload = {
        "output": [{
            "type": "message",
            "content": [{
                "text": "See https://one.test",
                "annotations": [{
                    "type": "url_citation",
                    "title": "One",
                    "url": "https://one.test",
                }],
            }],
        }]
    }
    monkeypatch.setattr(
        "mertina_agent.plugins.web.gateway.provider.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(payload),
    )
    provider = GatewayWebSearchProvider("https://gateway.test/v1", "key", "model")

    result = asyncio.run(provider.search("query"))

    assert len(result["data"]["web"]) == 1


def test_provider_extracts_urls_when_gateway_omits_annotations(monkeypatch):
    payload = {
        "output": [{
            "type": "message",
            "content": [{
                "text": "1. OpenAI https://openai.com/\n2. Platform https://platform.openai.com/",
                "annotations": [],
            }],
        }]
    }
    monkeypatch.setattr(
        "mertina_agent.plugins.web.gateway.provider.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(payload),
    )
    provider = GatewayWebSearchProvider("https://gateway.test/v1", "key", "model")

    result = asyncio.run(provider.search("query", 1))

    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://openai.com/"


def test_provider_returns_a_safe_failure(monkeypatch):
    def fail(*_args, **_kwargs):
        message = "offline"
        raise OSError(message)

    monkeypatch.setattr("mertina_agent.plugins.web.gateway.provider.request.urlopen", fail)
    provider = GatewayWebSearchProvider("https://gateway.test/v1", "key", "model")

    result = asyncio.run(provider.search("query"))

    assert result == {"success": False, "error": "Gateway web search failed: offline"}
