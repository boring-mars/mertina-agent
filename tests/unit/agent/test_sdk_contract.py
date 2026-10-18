"""Offline probes for the SDK behaviors the model-client boundary relies on."""

import asyncio
import json

import httpx2
import pytest
from openai import APIStatusError, AsyncOpenAI, Omit


@pytest.fixture(autouse=True)
def clean_sdk_environment(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_ADMIN_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_CUSTOM_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)


def _completion():
    return {
        "id": "chatcmpl-offline",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
    }


@pytest.mark.parametrize("api_key", [None, "settings-key"])
def test_sdk_options_override_injected_config_without_mutating_or_closing_it(monkeypatch, api_key):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "environment-admin-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://environment.invalid/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "environment-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "environment-project")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "authorization: Bearer custom-environment-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=_completion())

    async def scenario():
        async with AsyncOpenAI(
            api_key="injected-key",
            base_url="https://injected.invalid/other",
            timeout=999,
            max_retries=4,
            default_headers={"authorization": "Bearer injected-header-key"},
            default_query={"injected": "query"},
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        ) as external:
            derived = external.with_options(
                base_url="https://settings.invalid/v1",
                api_key=api_key or "unused-placeholder",
                timeout=7.5,
                max_retries=0,
                provider=None,
                set_default_headers={},
                set_default_query={},
            )
            headers = {
                name: Omit()
                for name in derived.default_headers
                if name.lower() in {"authorization", "openai-organization", "openai-project"}
            }
            headers["Authorization"] = f"Bearer {api_key}" if api_key else Omit()
            result = await derived.chat.completions.create(
                model="settings-model",
                messages=[{"role": "user", "content": "hello"}],
                stream=False,
                extra_headers=headers,
            )
            assert result.choices[0].message.content == "hello"
            assert external.api_key == "injected-key"
            assert str(external.base_url) == "https://injected.invalid/other/"
            assert external.max_retries == 4
            assert external.timeout == 999
            assert not external.is_closed()
            # Options views share the external HTTP resource; callers must not close the view.
            assert not derived.is_closed()
        assert external.is_closed()
        assert derived.is_closed()

    asyncio.run(scenario())
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://settings.invalid/v1/chat/completions"
    assert request.headers.get("authorization") == (f"Bearer {api_key}" if api_key else None)
    assert "openai-organization" not in request.headers
    assert "openai-project" not in request.headers
    assert all(value == 7.5 for value in request.extensions["timeout"].values())
    assert json.loads(request.content) == {
        "model": "settings-model",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }


@pytest.mark.parametrize("status", [401, 429, 503])
def test_sdk_max_retries_zero_makes_exactly_one_http_attempt(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(status, json={"error": {"message": "offline failure"}})

    async def scenario():
        async with AsyncOpenAI(
            api_key="offline-key",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        ) as client:
            with pytest.raises(APIStatusError) as raised:
                await client.chat.completions.create(
                    model="test-model", messages=[{"role": "user", "content": "hello"}]
                )
            assert raised.value.status_code == status

    asyncio.run(scenario())
    assert len(requests) == 1


@pytest.mark.parametrize("bad_count", [True, "12", -1, 1.5])
def test_sdk_keeps_invalid_usage_counts_visible_to_transport(bad_count):
    response = _completion()
    response["usage"] = {"prompt_tokens": bad_count}

    async def scenario():
        async with AsyncOpenAI(
            api_key="offline-key",
            max_retries=0,
            http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=response))
            ),
        ) as client:
            result = await client.chat.completions.create(
                model="test-model", messages=[{"role": "user", "content": "hello"}]
            )
            assert type(result.usage.prompt_tokens) is type(bad_count)
            assert result.usage.prompt_tokens == bad_count
            assert getattr(result.usage, "completion_tokens", None) is None

    asyncio.run(scenario())


def test_sdk_does_not_invent_missing_tool_call_ids():
    response = _completion()
    response["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
    }
    response["choices"][0]["finish_reason"] = "tool_calls"

    async def scenario():
        async with AsyncOpenAI(
            api_key="offline-key",
            http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=response))
            ),
        ) as client:
            result = await client.chat.completions.create(
                model="test-model", messages=[{"role": "user", "content": "hello"}]
            )
            assert getattr(result.choices[0].message.tool_calls[0], "id", None) is None

    asyncio.run(scenario())
