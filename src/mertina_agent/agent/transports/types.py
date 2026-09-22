"""Typed text/tool inputs and provider-independent completion results.

Adapted from Hermes agent/transports/types.py at 9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1.
Copyright (c) 2025 Nous Research. Distributed under the MIT license; see
LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-model-transport.md.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict, cast

from mertina_agent.exceptions import ModelResponseError

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]


class SystemMessage(TypedDict):
    """A caller-selected system instruction; its role is never rewritten."""

    role: Literal["system"]
    content: str


class DeveloperMessage(TypedDict):
    """A developer instruction for an endpoint that supports this role."""

    role: Literal["developer"]
    content: str


class UserMessage(TypedDict):
    """A text-only user message."""

    role: Literal["user"]
    content: str


class FunctionCall(TypedDict):
    """The wire representation of function arguments, without JSON interpretation."""

    name: str
    arguments: str


class ToolCallMessage(TypedDict):
    """A function call replayed as part of an assistant history message."""

    id: str
    type: Literal["function"]
    function: FunctionCall


class AssistantMessage(TypedDict):
    """Assistant text, function calls or refusal in caller-owned history."""

    role: Literal["assistant"]
    content: NotRequired[str | None]
    tool_calls: NotRequired[Sequence[ToolCallMessage] | None]
    refusal: NotRequired[str | None]


class ToolMessage(TypedDict):
    """A tool result correlated with an earlier assistant function call."""

    role: Literal["tool"]
    content: str
    tool_call_id: str


type ChatMessage = SystemMessage | DeveloperMessage | UserMessage | AssistantMessage | ToolMessage


class FunctionDefinition(TypedDict):
    """A function's standard description and JSON parameter schema."""

    name: str
    description: NotRequired[str]
    parameters: NotRequired[JsonObject]


class ToolDefinition(TypedDict):
    """A standard function declaration; no provider-specific options are accepted."""

    type: Literal["function"]
    function: FunctionDefinition


def _validate_nonempty_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        message = f"Response {field} must be a non-empty string."
        raise ModelResponseError(message)


def _validate_text(value: object, field: str, *, allow_none: bool = False) -> None:
    if not isinstance(value, str) and not (allow_none and value is None):
        message = f"Response {field} must be text" + (" or null." if allow_none else ".")
        raise ModelResponseError(message)


@dataclass(frozen=True)
class ToolCall:
    """A validated function request, not a promise that its arguments are executable.

    Unlike Hermes, this layer has no later ID repair pass. Missing IDs therefore
    fail here rather than producing history that cannot correlate tool results.
    Arguments deliberately remain opaque; even malformed JSON must be preserved.
    """

    id: str
    name: str
    arguments: str

    def __post_init__(self) -> None:
        """Enforce the same contract for direct construction and provider parsing."""
        _validate_nonempty_text(self.id, "tool call id")
        _validate_nonempty_text(self.name, "tool call name")
        _validate_text(self.arguments, "tool call arguments")


@dataclass(frozen=True)
class Usage:
    """Reported token counts; unknown counts remain distinct from measured zero."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        """Reject malformed counts without inferring totals or coercing booleans."""
        for value in (self.prompt_tokens, self.completion_tokens, self.total_tokens):
            # bool is an int subclass, but true is not a valid token count.
            if value is not None and (type(value) is not int or value < 0):
                message = "Response usage counts must be non-negative integers or null."
                raise ModelResponseError(message)


@dataclass(frozen=True)
class StreamUpdate:
    """What one streamed chunk contributed that a consumer may display.

    Attributes:
        text: Visible text delta, withheld once the response started a tool call.
        tools_started: Tool names that became known in this chunk.
    """

    text: str | None = None
    tools_started: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedResponse:
    """One completion with explicit terminal metadata and immutable tool ordering.

    Refusal and truncation are observable outcomes, not fabricated successful
    text. Callers must inspect finish_reason before deciding to execute tools.
    """

    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    refusal: str | None = None
    usage: Usage | None = None

    def __post_init__(self) -> None:
        """Keep direct construction from bypassing the normalized result contract."""
        _validate_nonempty_text(self.finish_reason, "finish_reason")
        _validate_text(self.content, "content", allow_none=True)
        _validate_text(self.refusal, "refusal", allow_none=True)
        if not isinstance(self.tool_calls, tuple) or not all(
            isinstance(call, ToolCall) for call in self.tool_calls
        ):
            message = "Response tool_calls must be an immutable tuple of ToolCall values."
            raise ModelResponseError(message)
        # Widen at the runtime boundary: Python callers are not obliged to use mypy.
        if self.usage is not None and not isinstance(cast(object, self.usage), Usage):
            message = "Response usage must be a Usage value or null."
            raise ModelResponseError(message)
        if not (
            (self.content and self.content.strip())
            or self.tool_calls
            or (self.refusal and self.refusal.strip())
            or self.finish_reason in {"length", "content_filter"}
        ):
            message = "Response has no text, tool calls, refusal or truncation/filter indication."
            raise ModelResponseError(message)
