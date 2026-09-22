"""Minimal text/function Chat Completions conversion with strict local boundaries.

Adapted from Hermes agent/transports/chat_completions.py at
9d24f9c91f3bf6b9faad5436cb12d20a8c7d46a1. Copyright (c) 2025 Nous Research.
Distributed under the MIT license; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-model-transport.md for source symbols and deliberate cuts.
"""

import json
import math
from collections.abc import Mapping, Sequence
from typing import cast

from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessageParam,
    ChatCompletionToolUnionParam,
)
from openai.types.chat.completion_create_params import (
    CompletionCreateParamsNonStreaming,
    CompletionCreateParamsStreaming,
)

from mertina_agent.agent.transports.base import ProviderTransport, StreamAccumulator
from mertina_agent.agent.transports.types import (
    ChatMessage,
    JsonValue,
    NormalizedResponse,
    StreamUpdate,
    ToolCall,
    ToolDefinition,
    Usage,
)
from mertina_agent.exceptions import ModelInputError, ModelResponseError


def _input_mapping(value: object, allowed: set[str], context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or key not in allowed for key in value
    ):
        message = f"{context} must be an object containing only supported fields."
        raise ModelInputError(message)
    return cast(Mapping[str, object], value)


def _input_sequence(value: object, context: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        message = f"{context} must be a sequence."
        raise ModelInputError(message)
    return value


def _input_text(value: object, context: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        message = f"{context} must be " + ("non-empty text." if nonempty else "text.")
        raise ModelInputError(message)
    return value


def _copy_json(value: object, ancestors: set[int]) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in ancestors:
            message = "Tool parameter schema must not contain circular references."
            raise ModelInputError(message)
        ancestors.add(identity)
        try:
            if isinstance(value, list):
                return [_copy_json(item, ancestors) for item in value]
            if all(isinstance(key, str) for key in value):
                return {key: _copy_json(item, ancestors) for key, item in value.items()}
        finally:
            ancestors.remove(identity)
    message = "Tool parameter schema must contain finite JSON values and string object keys."
    raise ModelInputError(message)


def _convert_tool_call(value: object) -> dict[str, object]:
    call = _input_mapping(value, {"id", "type", "function"}, "Assistant tool call")
    if call.get("type") != "function":
        message = "Assistant tool call type must be function."
        raise ModelInputError(message)
    function = _input_mapping(call.get("function"), {"name", "arguments"}, "Tool call function")
    return {
        "id": _input_text(call.get("id"), "Tool call id", nonempty=True),
        "type": "function",
        "function": {
            "name": _input_text(function.get("name"), "Tool call name", nonempty=True),
            "arguments": _input_text(function.get("arguments"), "Tool call arguments"),
        },
    }


def _convert_message(value: object) -> ChatCompletionMessageParam:
    message = _input_mapping(
        value, {"role", "content", "tool_calls", "tool_call_id", "refusal"}, "Message"
    )
    role = message.get("role")
    if role not in ("system", "developer", "user", "assistant", "tool"):
        error = "Message role is unsupported; use system, developer, user, assistant or tool."
        raise ModelInputError(error)
    allowed = {"role", "content"}
    if role == "assistant":
        allowed.update({"tool_calls", "refusal"})
    elif role == "tool":
        allowed.add("tool_call_id")
    _input_mapping(message, allowed, "Message for the selected role")

    converted: dict[str, object] = {"role": role}
    content = message.get("content")
    if role != "assistant":
        converted["content"] = _input_text(content, "Message content")
        if role == "tool":
            converted["tool_call_id"] = _input_text(
                message.get("tool_call_id"), "Tool result tool_call_id", nonempty=True
            )
    else:
        if content is not None:
            converted["content"] = _input_text(content, "Assistant content")
        elif "content" in message:
            converted["content"] = None
        raw_calls = message.get("tool_calls")
        tool_calls = [
            _convert_tool_call(call)
            for call in _input_sequence(
                () if raw_calls is None else raw_calls, "Assistant tool_calls"
            )
        ]
        # Strict endpoints reject empty tool_calls; omitting them preserves the
        # upstream regression fix without mutating reusable caller-owned history.
        if tool_calls:
            converted["tool_calls"] = tool_calls
        refusal = message.get("refusal")
        if refusal is not None:
            converted["refusal"] = _input_text(refusal, "Assistant refusal")
        if content is None and not tool_calls and not refusal:
            error = "Assistant history requires text, function calls or a refusal."
            raise ModelInputError(error)

    # SDK TypedDicts also admit modalities and provider options. This cast is
    # restricted to the boundary after validating our smaller supported subset.
    return cast(ChatCompletionMessageParam, converted)


def _response_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        message = f"Response {context} must be an object."
        raise ModelResponseError(message)
    return cast(Mapping[str, object], value)


def _response_text(value: object, context: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        message = f"Response {context} must be " + ("text or null." if nullable else "text.")
        raise ModelResponseError(message)
    return value


def _normalize_tool_call(value: object) -> ToolCall:
    call = _response_mapping(value, "tool call")
    if call.get("type") != "function":
        message = "Response tool call type must be function."
        raise ModelResponseError(message)
    function = _response_mapping(call.get("function"), "tool call function")
    # No dropped calls, fabricated IDs or argument repair: the future executor
    # needs both the original payload and reliable history correlation.
    return ToolCall(
        id=cast(str, _response_text(call.get("id"), "tool call id")),
        name=cast(str, _response_text(function.get("name"), "tool call name")),
        arguments=cast(str, _response_text(function.get("arguments"), "tool call arguments")),
    )


def _normalize_usage(value: object) -> Usage | None:
    if value is None:
        return None
    usage = _response_mapping(value, "usage")
    counts: list[int | None] = []
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        count = usage.get(field)
        if count is not None and (type(count) is not int or count < 0):
            message = "Response usage counts must be non-negative integers or null."
            raise ModelResponseError(message)
        counts.append(count)
    return Usage(prompt_tokens=counts[0], completion_tokens=counts[1], total_tokens=counts[2])


class ChatCompletionsTransport(ProviderTransport):
    """Convert the standard non-streaming text and function-call protocol subset."""

    def convert_messages(self, messages: Sequence[ChatMessage]) -> list[ChatCompletionMessageParam]:
        """Copy history without role rewriting or silently stripping unsupported data.

        Raises:
            ModelInputError: If history is empty or contains unsupported input.
        """
        values = _input_sequence(messages, "Messages")
        if not values:
            message = "Messages must contain at least one message."
            raise ModelInputError(message)
        return [_convert_message(value) for value in values]

    def convert_tools(self, tools: Sequence[ToolDefinition]) -> list[ChatCompletionToolUnionParam]:
        """Validate function declarations and detach nested JSON schemas from callers."""
        converted: list[ChatCompletionToolUnionParam] = []
        for value in _input_sequence(tools, "Tools"):
            tool = _input_mapping(value, {"type", "function"}, "Tool definition")
            if tool.get("type") != "function":
                message = "Tool definition type must be function."
                raise ModelInputError(message)
            function = _input_mapping(
                tool.get("function"), {"name", "description", "parameters"}, "Tool function"
            )
            declaration: dict[str, object] = {
                "name": _input_text(function.get("name"), "Tool function name", nonempty=True)
            }
            if "description" in function:
                declaration["description"] = _input_text(
                    function["description"], "Tool function description"
                )
            if "parameters" in function:
                parameters = function["parameters"]
                if not isinstance(parameters, dict):
                    message = "Tool parameters must be a JSON schema object."
                    raise ModelInputError(message)
                declaration["parameters"] = _copy_json(parameters, set())
            converted.append(
                cast(ChatCompletionToolUnionParam, {"type": "function", "function": declaration})
            )
        return converted

    def build_kwargs(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> CompletionCreateParamsNonStreaming:
        """Build one non-streaming request, using the protocol's single-choice default."""
        api_kwargs: CompletionCreateParamsNonStreaming = {
            "model": _input_text(model, "Model", nonempty=True),
            "messages": self.convert_messages(messages),
            "stream": False,
        }
        converted_tools = self.convert_tools(tools)
        if converted_tools:
            api_kwargs["tools"] = converted_tools
        return api_kwargs

    def build_stream_kwargs(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
        *,
        include_usage: bool = True,
    ) -> CompletionCreateParamsStreaming:
        """Build one streaming request with the same validation as :meth:`build_kwargs`.

        ``include_usage`` asks for a final usage chunk; strict endpoints that
        reject ``stream_options`` are called again without it.
        """
        base = self.build_kwargs(model, messages, tools)
        api_kwargs: CompletionCreateParamsStreaming = {
            "model": base["model"],
            "messages": base["messages"],
            "stream": True,
        }
        if "tools" in base:
            api_kwargs["tools"] = base["tools"]
        if include_usage:
            api_kwargs["stream_options"] = {"include_usage": True}
        return api_kwargs

    def stream_accumulator(self) -> StreamAccumulator:
        """Return a fresh accumulator for one streamed response."""
        return ChatCompletionsStreamAccumulator()

    def normalize_response(self, response: object) -> NormalizedResponse:
        """Normalize one choice while preserving refusal, truncation and unknown usage.

        Raises:
            ModelResponseError: If required structure or any tool/count is malformed.
        """
        # SDK parsing is intentionally permissive. Dump without coercion/warnings
        # and validate ourselves so malformed provider fields remain protocol errors.
        if isinstance(response, ChatCompletion):
            response = response.model_dump(mode="python", warnings=False)
        data = _response_mapping(response, "root")
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            message = "Response must contain exactly one choice."
            raise ModelResponseError(message)
        choice = _response_mapping(choices[0], "choice")
        message_data = _response_mapping(choice.get("message"), "message")
        if message_data.get("role") != "assistant":
            message = "Response message role must be assistant."
            raise ModelResponseError(message)
        finish_reason = _response_text(choice.get("finish_reason"), "finish_reason")
        raw_calls = message_data.get("tool_calls")
        if raw_calls is not None and not isinstance(raw_calls, list):
            message = "Response tool_calls must be an array or null."
            raise ModelResponseError(message)
        tool_calls = tuple(_normalize_tool_call(call) for call in (raw_calls or []))
        return NormalizedResponse(
            content=_response_text(message_data.get("content"), "content", nullable=True),
            tool_calls=tool_calls,
            finish_reason=cast(str, finish_reason),
            refusal=_response_text(message_data.get("refusal"), "refusal", nullable=True),
            usage=_normalize_usage(data.get("usage")),
        )


def _optional_text(mapping: Mapping[str, object], key: str, context: str) -> str | None:
    return _response_text(mapping.get(key), context, nullable=True)


class _ToolCallAccumulator:
    """Assemble streamed tool-call deltas into complete tool calls.

    Copied from Hermes agent/chat_completion_helpers.py (``_ToolCallAccumulator``)
    at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Ollama-compatible endpoints reuse
    index 0 for every call of a parallel batch and tell them apart only by ID, so a
    new ID at an already-seen index is redirected to a fresh slot. Argument
    fragments are collected per slot and joined once, because ``+=`` per chunk is
    quadratic on large arguments.
    """

    def __init__(self) -> None:
        self.slots: dict[int, dict[str, str]] = {}
        self._notified: set[int] = set()
        self._last_id_at_index: dict[int, str] = {}
        self._active_slot_by_index: dict[int, int] = {}
        self._argument_parts: dict[int, list[str]] = {}

    def feed(self, delta: object) -> str | None:
        """Merge one tool-call delta; return the tool name the first time it is known."""
        data = _response_mapping(delta, "tool call delta")
        raw_index = data.get("index")
        if raw_index is None:
            raw_index = 0
        if type(raw_index) is not int or raw_index < 0:
            message = "Response tool call delta index must be a non-negative integer."
            raise ModelResponseError(message)
        call_id = _optional_text(data, "id", "tool call delta id") or ""

        self._active_slot_by_index.setdefault(raw_index, raw_index)
        if (
            call_id
            and raw_index in self._last_id_at_index
            and call_id != self._last_id_at_index[raw_index]
        ):
            self._active_slot_by_index[raw_index] = max(self.slots, default=-1) + 1
        if call_id:
            self._last_id_at_index[raw_index] = call_id
        slot = self._active_slot_by_index[raw_index]

        entry = self.slots.setdefault(slot, {"id": "", "name": "", "arguments": ""})
        parts = self._argument_parts.setdefault(slot, [])
        if call_id:
            entry["id"] = call_id
        function_data = data.get("function")
        if function_data is not None:
            function = _response_mapping(function_data, "tool call delta function")
            name = _optional_text(function, "name", "tool call delta name")
            if name:
                # Assignment, not +=: some providers resend the full name every chunk.
                entry["name"] = name
            arguments = _optional_text(function, "arguments", "tool call delta arguments")
            if arguments:
                parts.append(arguments)
        if entry["name"] and slot not in self._notified:
            self._notified.add(slot)
            return entry["name"]
        return None

    def materialize(self) -> list[dict[str, str]]:
        """Join buffered argument fragments and return the entries in slot order."""
        for slot, parts in self._argument_parts.items():
            self.slots[slot]["arguments"] = "".join(parts)
        return [self.slots[slot] for slot in sorted(self.slots)]


class ChatCompletionsStreamAccumulator(StreamAccumulator):
    """Assemble streamed chunks into the same result a non-streaming call returns.

    The end-of-stream rules are copied from Hermes ``_finish_chat_stream``: a
    stream that ends without a finish reason is a dropped connection, not a
    completion, so it is never reported as a normal answer. Where Hermes turns a
    dropped stream into a continuation stub, Mertina raises
    :class:`ModelResponseError`, which the agent retries. Argument repair and
    reasoning deltas are left out.
    """

    def __init__(self) -> None:
        """Create an empty accumulator."""
        self._content_parts: list[str] = []
        self._refusal_parts: list[str] = []
        self._tool_calls = _ToolCallAccumulator()
        self._finish_reason: str | None = None
        self._usage: Usage | None = None

    def feed(self, chunk: object) -> StreamUpdate:
        """Merge one chunk and return what it adds for display.

        Raises:
            ModelResponseError: If the chunk is malformed.
        """
        if isinstance(chunk, ChatCompletionChunk):
            chunk = chunk.model_dump(mode="python", warnings=False)
        data = _response_mapping(chunk, "chunk")
        usage = _normalize_usage(data.get("usage"))
        if usage is not None:
            self._usage = usage
        choices = data.get("choices")
        if choices is None or choices == []:
            # A choiceless chunk carries usage (``include_usage``) or nothing.
            return StreamUpdate()
        if not isinstance(choices, list) or len(choices) != 1:
            message = "Response chunk must contain exactly one choice."
            raise ModelResponseError(message)
        choice = _response_mapping(choices[0], "chunk choice")
        finish_reason = _optional_text(choice, "finish_reason", "finish_reason")
        if finish_reason:
            self._finish_reason = finish_reason
        delta_data = choice.get("delta")
        delta = _response_mapping(delta_data if delta_data is not None else {}, "chunk delta")

        refusal = _optional_text(delta, "refusal", "refusal")
        if refusal:
            self._refusal_parts.append(refusal)
        tools_started: list[str] = []
        raw_calls = delta.get("tool_calls")
        if raw_calls is not None:
            if not isinstance(raw_calls, list):
                message = "Response tool call deltas must be an array or null."
                raise ModelResponseError(message)
            for raw_call in raw_calls:
                name = self._tool_calls.feed(raw_call)
                if name is not None:
                    tools_started.append(name)
        text = _optional_text(delta, "content", "content")
        visible: str | None = None
        if text:
            self._content_parts.append(text)
            # Tool-call turns do not stream their preamble, as in Hermes.
            if not self._tool_calls.slots:
                visible = text
        return StreamUpdate(text=visible, tools_started=tuple(tools_started))

    def finish(self) -> NormalizedResponse:
        """Validate the whole stream and return the normalized result.

        Raises:
            ModelResponseError: If the stream was empty, dropped mid-way, or its
                assembled result violates the response contract.
        """
        content = "".join(self._content_parts) or None
        refusal = "".join(self._refusal_parts) or None
        entries = self._tool_calls.materialize()
        finish_reason = self._finish_reason
        if finish_reason is None and not (content or refusal or entries):
            message = "Model stream ended without any content or finish reason."
            raise ModelResponseError(message)

        truncated_arguments = False
        for entry in entries:
            arguments = entry["arguments"]
            if arguments.strip():
                try:
                    json.loads(arguments)
                except json.JSONDecodeError:
                    truncated_arguments = True
            elif finish_reason is None:
                # A name with no argument bytes and no finish reason was cut off.
                truncated_arguments = True
        if finish_reason is None and truncated_arguments:
            message = "Model stream ended in the middle of a tool call."
            raise ModelResponseError(message)
        if finish_reason is None and not entries and self._usage is None:
            # A final usage chunk proves the provider finished; without it the
            # text may be cut short and must not be reported as complete.
            message = "Model stream ended before the response was complete."
            raise ModelResponseError(message)

        return NormalizedResponse(
            content=content,
            tool_calls=tuple(
                ToolCall(id=entry["id"], name=entry["name"], arguments=entry["arguments"])
                for entry in entries
            ),
            finish_reason="length" if truncated_arguments else (finish_reason or "stop"),
            refusal=refusal,
            usage=self._usage,
        )
