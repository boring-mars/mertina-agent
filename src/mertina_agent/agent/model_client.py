"""One asynchronous, non-streaming model request with explicit resource ownership.

Adapted from Hermes Agent's explicit client options and non-streaming dispatch.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt
and docs/sources/hermes-model-transport.md for the pinned source and reductions.
"""

from collections.abc import Sequence
from json import JSONDecodeError
from types import TracebackType
from typing import Protocol, Self

import openai
from openai import AsyncOpenAI, DefaultAsyncHttpxClient, Omit
from pydantic import ValidationError

from mertina_agent.agent.transports import ChatCompletionsTransport, ProviderTransport
from mertina_agent.agent.transports.types import ChatMessage, NormalizedResponse, ToolDefinition
from mertina_agent.config import Settings
from mertina_agent.exceptions import ConfigurationError, ModelRequestError, ModelResponseError

_NO_AUTH_KEY = "mertina-no-auth-placeholder"


class ModelClientProtocol(Protocol):
    """The model-call boundary the agent loop depends on.

    :class:`ModelClient` implements it against a real endpoint; tests supply a
    scripted fake at this boundary instead of patching the SDK.
    """

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolDefinition] = (),
    ) -> NormalizedResponse:
        """Send one request and return its normalized result without executing tools."""
        ...

    async def aclose(self) -> None:
        """Release resources the client owns."""
        ...


class ModelClient:
    """Call a standard Chat Completions endpoint without owning conversation state.

    Settings take precedence over an injected SDK's configuration. Such SDKs
    remain caller-owned, including their shared HTTP connection pool. Custom
    HTTP-level authentication is rejected because SDK options cannot override it
    reliably. Injected HTTP transports are trusted caller-supplied code.

    Use an async context manager or call ``aclose`` explicitly. The network
    timeout applies to individual I/O operations, not to an entire agent turn.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: ProviderTransport | None = None,
        sdk_client: AsyncOpenAI | None = None,
    ) -> None:
        """Bind validated settings, a pure transport and an asynchronous SDK.

        Raises:
            ConfigurationError: If settings or the injected SDK cannot uphold
                the explicit endpoint, authentication and single-attempt policy.
        """
        # Revalidate copies made with Pydantic's unchecked model_copy/construct.
        # This must happen before allocating an owned network client.
        try:
            self._settings = Settings.model_validate(settings.model_dump())
        except ValidationError as exc:
            message = "Invalid model client configuration"
            raise ConfigurationError(message) from exc

        self._transport = transport if transport is not None else ChatCompletionsTransport()
        self._closed = False
        self._owns_sdk = sdk_client is None
        secret = self._settings.llm_api_key
        self._key = secret.get_secret_value() if secret is not None else ""
        key = self._key or _NO_AUTH_KEY

        if sdk_client is None:
            self._sdk = AsyncOpenAI(
                api_key=key,
                admin_api_key="",
                organization="",
                project="",
                webhook_secret="",
                base_url=self._settings.llm_base_url,
                timeout=self._settings.llm_timeout_s,
                # Later agent policy owns retries; do not multiply its attempts.
                max_retries=0,
                default_headers={"Authorization": ""},
                # A 307 redirect otherwise triggers a second HTTP dispatch even
                # with SDK retries disabled. Report it to the caller instead.
                http_client=DefaultAsyncHttpxClient(follow_redirects=False),
            )
        else:
            self._check_injected_client(sdk_client)
            try:
                self._sdk = sdk_client.with_options(
                    api_key=key,
                    base_url=self._settings.llm_base_url,
                    timeout=self._settings.llm_timeout_s,
                    max_retries=0,
                    provider=None,
                    set_default_headers={"Authorization": ""},
                    set_default_query={},
                )
            except (TypeError, ValueError, openai.OpenAIError) as exc:
                message = "Injected SDK does not support the required configuration overrides"
                raise ConfigurationError(message) from exc

        # SDK 3.x may inherit OPENAI_CUSTOM_HEADERS and organization/project
        # variables even when api_key is explicit. Remove ambient SDK headers at
        # the request boundary, including differently-cased Authorization keys.
        self._headers: dict[str, str | Omit] = {name: Omit() for name in self._sdk.default_headers}
        self._headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key}" if self._key else Omit(),
            }
        )

    @staticmethod
    def _check_injected_client(sdk_client: AsyncOpenAI) -> None:
        if not isinstance(sdk_client, openai.AsyncOpenAI) or sdk_client.is_closed():
            message = "Injected SDK must be an open AsyncOpenAI client"
            raise ConfigurationError(message)
        # The SDK exposes no public HTTP-client getter. This read-only guard is
        # intentionally confined to the tested SDK boundary: HTTP defaults are
        # merged *after* SDK Omit headers and could reintroduce authentication.
        # Never mutate or close the caller's underlying client to work around it.
        http_client = sdk_client._client
        safe_headers = {"accept", "accept-encoding", "connection", "user-agent", "content-type"}
        if (
            http_client.auth is not None
            or http_client.follow_redirects
            or bool(http_client.params)
            or bool(http_client.cookies)
            or any(name.lower() not in safe_headers for name in http_client.headers)
        ):
            message = "Injected HTTP defaults must not enable redirects, auth or extra request data"
            raise ConfigurationError(message)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolDefinition] = (),
    ) -> NormalizedResponse:
        """Send one request and normalize its result without executing tools.

        Raises:
            ModelInputError: If input is outside the supported text contract.
            ModelRequestError: For timeout, connection, HTTP or closed failures.
            ModelResponseError: If the provider response is malformed.

        Cancellation and unknown programming errors propagate unchanged.
        """
        self._ensure_open()
        kwargs = self._transport.build_kwargs(self._settings.llm_model, messages, tools)
        try:
            # SDK models are not our protocol validator. Use its supported raw
            # response API to validate wire values before any model conversion
            # or serialization, including malformed counts and unknown reasons.
            response = await self._sdk.chat.completions.with_raw_response.create(
                **kwargs, extra_headers=self._headers
            )
            payload: object = response.http_response.json()
        except openai.APITimeoutError as exc:
            message = "Model request timed out"
            raise ModelRequestError(message, kind="timeout") from exc
        except openai.APIConnectionError as exc:
            message = "Could not connect to the model endpoint"
            raise ModelRequestError(message, kind="connection") from exc
        except openai.APIStatusError as exc:
            message = f"Model endpoint returned HTTP {exc.status_code}"
            raise ModelRequestError(
                message,
                kind="http",
                status_code=exc.status_code,
                retry_after=exc.response.headers.get("retry-after"),
            ) from exc
        except (openai.APIResponseValidationError, JSONDecodeError, UnicodeDecodeError) as exc:
            message = "Model endpoint returned an invalid response"
            raise ModelResponseError(message) from exc
        return self._transport.normalize_response(payload)

    def _ensure_open(self) -> None:
        if self._closed or self._sdk.is_closed():
            message = "Model client is closed"
            raise ModelRequestError(message, kind="closed")

    async def aclose(self) -> None:
        """Close this wrapper and its owned SDK, never caller-owned resources."""
        if self._closed:
            return
        # Do not leave a failed/partially closed client available for requests.
        self._closed = True
        if self._owns_sdk:
            await self._sdk.close()

    async def __aenter__(self) -> Self:
        """Enter an open model client context."""
        self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release owned resources on success, failure or task cancellation."""
        await self.aclose()
