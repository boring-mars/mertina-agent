"""The provider call of one loop iteration.

Adapted from Hermes agent/turn_api_call.py (``perform_api_call``) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

At this checkpoint the call is a single non-streaming request. Interruption of
an in-flight request and streaming are added in later checkpoints; the Nous
rate guard, middleware, relay and MoA handshake are left out.
"""

from collections.abc import Sequence

from mertina_agent.agent.model_client import ModelClientProtocol
from mertina_agent.agent.transports.types import ChatMessage, NormalizedResponse, ToolDefinition


async def perform_api_call(
    model_client: ModelClientProtocol,
    api_messages: Sequence[ChatMessage],
    tools: Sequence[ToolDefinition],
) -> NormalizedResponse:
    """Issue one model request for the current iteration.

    Raises:
        ModelInputError: If the history violates the text contract.
        ModelRequestError: For timeout, connection, HTTP or closed failures.
        ModelResponseError: If the provider response is malformed.
    """
    return await model_client.complete(api_messages, tools=tools)
