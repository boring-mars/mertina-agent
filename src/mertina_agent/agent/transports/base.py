"""Abstract conversion boundary, independent of I/O and client lifecycle.

Adapted from Hermes agent/transports/base.py at 9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1.
Copyright (c) 2025 Nous Research. Distributed under the MIT license; see
LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-model-transport.md.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam
from openai.types.chat.completion_create_params import CompletionCreateParamsNonStreaming

from mertina_agent.agent.transports.types import ChatMessage, NormalizedResponse, ToolDefinition


class ProviderTransport(ABC):
    """Convert standard model data without credentials, networking or runtime state."""

    @abstractmethod
    def convert_messages(self, messages: Sequence[ChatMessage]) -> list[ChatCompletionMessageParam]:
        """Validate and copy caller-owned history into the supported wire format."""

    @abstractmethod
    def convert_tools(self, tools: Sequence[ToolDefinition]) -> list[ChatCompletionToolUnionParam]:
        """Validate and deeply copy standard function declarations."""

    @abstractmethod
    def build_kwargs(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> CompletionCreateParamsNonStreaming:
        """Build a non-streaming single-choice request, with no provider extras."""

    @abstractmethod
    def normalize_response(self, response: object) -> NormalizedResponse:
        """Validate one provider response, raising ModelResponseError if malformed."""
