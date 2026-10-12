"""Exercise ModelClient through the real SDK and an offline HTTP boundary."""

import asyncio
import json
import logging

import httpx2
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    DefaultAsyncHttpxClient,
)
from openai.resources.chat.completions import AsyncCompletions
from pydantic import SecretStr

from mertina_agent.agent import model_client
from mertina_agent.agent.model_client import ModelClient
from mertina_agent.config import Settings
from mertina_agent.exceptions import (
    ConfigurationError,
    ModelInputError,
    ModelRequestError,
    ModelResponseError,
)


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_ADMIN_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_CUSTOM_HEADERS",
        "MERTINA_LLM_API_KEY",
        "MERTINA_LLM_BASE_URL",
        "MERTINA_LLM_MODEL",
        "MERTINA_LLM_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(api_key="settings-key"):
    return Settings(
        llm_base_url="https://settings.invalid/v1",
        llm_api_key=SecretStr(api_key) if api_key is not None else None,
        llm_model="settings-model",
        llm_timeout_s=7.5,
    )


def _completion():
    return {
        "id": "chatcmpl-offline",
        "object": "chat.completion",
        "created": 0,
        "model": "settings-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _sdk(handler, **kwargs):
    return AsyncOpenAI(
        api_key="injected-key",
        base_url="https://injected.invalid/other",
        timeout=999,
        max_retries=4,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        **kwargs,
    )


def _owned_sdk_factory(monkeypatch, handler):
    created = []

    def create_http_client(**kwargs):
        return DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler), **kwargs)

    def create_sdk(**kwargs):
        client = AsyncOpenAI(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(model_client, "DefaultAsyncHttpxClient", create_http_client)
    monkeypatch.setattr(model_client, "AsyncOpenAI", create_sdk)
    return created


@pytest.mark.parametrize("api_key", [None, "", "settings-key"])
def test_explicit_settings_override_injected_sdk_and_ambient_credentials(monkeypatch, api_key):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "environment-admin-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://environment.invalid/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "environment-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "environment-project")
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "authorization: Bearer environment-custom-key\nOpenAI-Project: environment-header-project",
    )
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=_completion())

    async def scenario():
        async with _sdk(
            respond,
            default_headers={"authorization": "Bearer injected-header-key"},
            default_query={"api-key": "injected-query-key"},
        ) as external:
            async with ModelClient(_settings(api_key), sdk_client=external) as client:
                result = await client.complete([{"role": "user", "content": "first message"}])
                assert result.content == "hello"
                assert result.finish_reason == "stop"
                assert result.usage.prompt_tokens == 10
            assert not external.is_closed()
            assert external.api_key == "injected-key"
            assert external.max_retries == 4
            assert external.timeout == 999
            assert str(external.base_url) == "https://injected.invalid/other/"

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
        "messages": [{"role": "user", "content": "first message"}],
        "stream": False,
    }


@pytest.mark.parametrize("api_key", [None, "", "settings-key"])
def test_owned_sdk_never_uses_ambient_credentials(monkeypatch, api_key):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "environment-admin-key")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "authorization: Bearer environment-custom-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=_completion())

    created = _owned_sdk_factory(monkeypatch, respond)

    async def scenario():
        async with ModelClient(_settings(api_key)) as client:
            await client.complete([{"role": "user", "content": "hello"}])

    asyncio.run(scenario())
    assert len(created) == 1
    assert created[0].is_closed()
    assert len(requests) == 1
    assert requests[0].headers.get("authorization") == (f"Bearer {api_key}" if api_key else None)


def test_each_completion_uses_only_the_supplied_history_and_preserves_tool_protocol():
    requests = []
    response = _completion()
    response["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{"}},
            {"id": "call-2", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
        ],
    }
    response["choices"][0]["finish_reason"] = "tool_calls"
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=response)

    async def scenario():
        async with (
            _sdk(respond) as external,
            ModelClient(_settings(), sdk_client=external) as client,
        ):
            await client.complete([{"role": "user", "content": "first"}])
            result = await client.complete([{"role": "user", "content": "second"}], tools=tools)
            assert result.content is None
            assert [call.id for call in result.tool_calls] == ["call-1", "call-2"]
            assert [call.arguments for call in result.tool_calls] == ["{", "{}"]

    asyncio.run(scenario())
    assert len(requests) == 2
    assert requests[0]["messages"] == [{"role": "user", "content": "first"}]
    assert requests[1]["messages"] == [{"role": "user", "content": "second"}]
    assert requests[1]["tools"] == tools


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_http_errors_preserve_safe_metadata_without_retrying(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(
            status,
            json={"error": {"message": "private provider details"}},
            headers={"Retry-After": "17"},
        )

    async def scenario():
        async with (
            _sdk(respond) as external,
            ModelClient(_settings(), sdk_client=external) as client,
        ):
            with pytest.raises(ModelRequestError) as raised:
                await client.complete([{"role": "user", "content": "hello"}])
            assert raised.value.kind == "http"
            assert raised.value.status_code == status
            assert raised.value.retry_after == "17"
            assert isinstance(raised.value.__cause__, APIStatusError)

    asyncio.run(scenario())
    assert len(requests) == 1


def test_owned_sdk_does_not_follow_redirects_into_a_second_http_attempt(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/v1/chat/completions":
            return httpx2.Response(307, headers={"Location": "/redirect-target"})
        return httpx2.Response(200, json=_completion())

    created = _owned_sdk_factory(monkeypatch, respond)

    async def scenario():
        async with ModelClient(_settings()) as client:
            with pytest.raises(ModelRequestError) as raised:
                await client.complete([{"role": "user", "content": "hello"}])
            assert raised.value.kind == "http"
            assert raised.value.status_code == 307

    asyncio.run(scenario())
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    assert len(created) == 1
    assert created[0].is_closed()


def test_injected_sdk_with_redirects_enabled_is_rejected_without_closing_it():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=_completion())

    async def scenario():
        async with AsyncOpenAI(
            api_key="injected-key",
            http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(respond), follow_redirects=True
            ),
        ) as external:
            with pytest.raises(ConfigurationError):
                ModelClient(_settings(), sdk_client=external)
            assert not external.is_closed()

    asyncio.run(scenario())
    assert requests == []


@pytest.mark.parametrize(
    ("http_error", "kind", "sdk_error"),
    [
        (httpx2.ReadTimeout, "timeout", APITimeoutError),
        (httpx2.ConnectError, "connection", APIConnectionError),
    ],
)
def test_network_errors_have_distinct_kinds_without_retries(http_error, kind, sdk_error):
    requests = []

    def fail(request):
        requests.append(request)
        message = "private connection failure"
        raise http_error(message, request=request)

    async def scenario():
        async with _sdk(fail) as external, ModelClient(_settings(), sdk_client=external) as client:
            with pytest.raises(ModelRequestError) as raised:
                await client.complete([{"role": "user", "content": "hello"}])
            assert raised.value.kind == kind
            assert raised.value.status_code is None
            assert isinstance(raised.value.__cause__, sdk_error)

    asyncio.run(scenario())
    assert len(requests) == 1


@pytest.mark.parametrize("bad_count", [True, "12", -1, 1.5])
def test_invalid_usage_survives_real_sdk_parsing_and_raises_response_error(bad_count):
    response = _completion()
    response["usage"]["prompt_tokens"] = bad_count

    async def scenario():
        async with (
            _sdk(lambda _: httpx2.Response(200, json=response)) as external,
            ModelClient(_settings(), sdk_client=external) as client,
        ):
            with pytest.raises(ModelResponseError):
                await client.complete([{"role": "user", "content": "hello"}])

    asyncio.run(scenario())


def test_missing_tool_id_raises_response_error_after_real_sdk_parsing():
    response = _completion()
    response["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
    }
    response["choices"][0]["finish_reason"] = "tool_calls"

    async def scenario():
        async with (
            _sdk(lambda _: httpx2.Response(200, json=response)) as external,
            ModelClient(_settings(), sdk_client=external) as client,
        ):
            with pytest.raises(ModelResponseError):
                await client.complete([{"role": "user", "content": "hello"}])

    asyncio.run(scenario())


def test_successful_http_response_with_invalid_json_is_a_response_error():
    async def scenario():
        async with (
            _sdk(lambda _: httpx2.Response(200, content=b"{broken-json")) as external,
            ModelClient(_settings(), sdk_client=external) as client,
        ):
            with pytest.raises(ModelResponseError) as raised:
                await client.complete([{"role": "user", "content": "hello"}])
            assert raised.value.__cause__ is not None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "unsupported", "content": "hello"}],
        [{"role": "user", "content": [{"type": "image_url"}]}],
    ],
)
def test_invalid_input_fails_before_any_http_request(messages):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=_completion())

    async def scenario():
        async with (
            _sdk(respond) as external,
            ModelClient(_settings(), sdk_client=external) as client,
        ):
            with pytest.raises(ModelInputError):
                await client.complete(messages)

    asyncio.run(scenario())
    assert requests == []


@pytest.mark.parametrize(
    "http_kwargs",
    [{"headers": {"authorization": "http-default-key"}}, {"auth": ("user", "password")}],
)
def test_injected_http_auth_that_cannot_be_overridden_is_rejected(http_kwargs):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=_completion())

    async def scenario():
        async with AsyncOpenAI(
            api_key="injected-key",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond), **http_kwargs),
        ) as external:
            with pytest.raises(ConfigurationError):
                ModelClient(_settings(None), sdk_client=external)
            assert not external.is_closed()

    asyncio.run(scenario())
    assert requests == []


def test_injected_sdk_that_cannot_override_options_fails_before_dispatch(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=_completion())

    def unsupported_options(**_kwargs):
        message = "options unsupported"
        raise TypeError(message)

    async def scenario():
        async with _sdk(respond) as external:
            monkeypatch.setattr(external, "with_options", unsupported_options)
            with pytest.raises(ConfigurationError) as raised:
                ModelClient(_settings(), sdk_client=external)
            assert isinstance(raised.value.__cause__, TypeError)
            assert not external.is_closed()

    asyncio.run(scenario())
    assert requests == []


def test_non_sdk_injection_is_rejected_as_configuration_error():
    with pytest.raises(ConfigurationError, match="open AsyncOpenAI"):
        ModelClient(_settings(), sdk_client=object())


def test_already_closed_injected_sdk_is_rejected_before_dispatch():
    async def scenario():
        external = _sdk(lambda _: pytest.fail("No HTTP request should be sent"))
        await external.close()
        with pytest.raises(ConfigurationError, match="open AsyncOpenAI"):
            ModelClient(_settings(), sdk_client=external)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "update",
    [{"llm_base_url": "ftp://invalid.example"}, {"llm_model": ""}, {"llm_timeout_s": 0}],
)
def test_unchecked_invalid_settings_fail_before_allocating_a_client(monkeypatch, update):
    created = _owned_sdk_factory(monkeypatch, lambda _: httpx2.Response(200, json=_completion()))
    with pytest.raises(ConfigurationError):
        ModelClient(_settings().model_copy(update=update))
    assert created == []


@pytest.mark.parametrize("exceptional", [False, True])
def test_owned_sdk_is_closed_on_normal_and_exceptional_exit(monkeypatch, exceptional):
    created = _owned_sdk_factory(monkeypatch, lambda _: httpx2.Response(200, json=_completion()))

    async def scenario():
        client = ModelClient(_settings())
        try:
            async with client:
                await client.complete([{"role": "user", "content": "hello"}])
                if exceptional:
                    message = "caller error"
                    raise LookupError(message)
        except LookupError:
            assert exceptional
        assert len(created) == 1
        assert created[0].is_closed()
        await client.aclose()
        await client.aclose()
        with pytest.raises(ModelRequestError) as raised:
            await client.complete([{"role": "user", "content": "after close"}])
        assert raised.value.kind == "closed"

    asyncio.run(scenario())


def test_closing_wrapper_leaves_injected_sdk_usable_and_rejects_further_calls():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=_completion())

    async def scenario():
        async with _sdk(respond) as external:
            client = ModelClient(_settings(), sdk_client=external)
            await client.aclose()
            await client.aclose()
            with pytest.raises(ModelRequestError) as raised:
                await client.complete([{"role": "user", "content": "after close"}])
            assert raised.value.kind == "closed"
            assert not external.is_closed()
            result = await external.chat.completions.create(
                model="external-model", messages=[{"role": "user", "content": "external"}]
            )
            assert result.choices[0].message.content == "hello"

    asyncio.run(scenario())
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer injected-key"


def test_active_cancellation_propagates_and_context_closes_owned_sdk(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        stopped = asyncio.Event()
        requests = []

        async def pending(request):
            requests.append(request)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        created = _owned_sdk_factory(monkeypatch, pending)
        async with ModelClient(_settings()) as client:
            task = asyncio.create_task(client.complete([{"role": "user", "content": "hello"}]))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stopped.is_set()
        assert len(requests) == 1
        assert len(created) == 1
        assert created[0].is_closed()

    asyncio.run(scenario())


def test_unknown_sdk_programming_error_is_not_wrapped(monkeypatch):
    problem = RuntimeError("sdk programming bug")

    async def broken_create(_self, **_kwargs):
        raise problem

    monkeypatch.setattr(AsyncCompletions, "create", broken_create)

    async def scenario():
        async with (
            _sdk(lambda _: httpx2.Response(200, json=_completion())) as external,
            ModelClient(_settings(), sdk_client=external) as client,
        ):
            with pytest.raises(RuntimeError) as raised:
                await client.complete([{"role": "user", "content": "hello"}])
            assert raised.value is problem

    asyncio.run(scenario())


def test_errors_and_application_logs_do_not_expose_secrets_or_provider_bodies(caplog):
    secret = "mock-sensitive-api-key"
    prompt = "complete private user message"
    body_text = f"provider echoed {secret} and {prompt}"
    caplog.set_level(logging.DEBUG, logger="mertina_agent")

    async def scenario():
        async with (
            _sdk(
                lambda _: httpx2.Response(429, json={"error": {"message": body_text}})
            ) as external,
            ModelClient(_settings(secret), sdk_client=external) as client,
        ):
            with pytest.raises(ModelRequestError) as raised:
                await client.complete([{"role": "user", "content": prompt}])
            assert raised.value.__cause__ is not None
            public_text = str(raised.value)
            app_logs = "\n".join(
                record.getMessage()
                for record in caplog.records
                if record.name.startswith("mertina_agent")
            )
            for sensitive in (secret, prompt, body_text):
                assert sensitive not in public_text
                assert sensitive not in app_logs

    asyncio.run(scenario())
