"""OpenAI compatible Chat Completions transport."""

from __future__ import annotations

from typing import Any

from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse, ToolCall, Usage


_INTERNAL_MESSAGE_KEYS = {
    "reasoning", "reasoning_content", "reasoning_details", "provider_data",
    "anthropic_content_blocks", "bedrock_content_blocks", "codex_reasoning_items",
    "codex_message_items", "response_item_id", "call_id", "effect_disposition",
}


def router_timeout_shim_may_follow(text: str) -> bool:
    return False


def is_router_timeout_shim(_response: Any) -> bool:
    return False


def _model_consumes_thought_signature(_model: Any) -> bool:
    return False


class ChatCompletionsTransport(ProviderTransport):
    @property
    def api_mode(self) -> str:
        return "chat_completions"

    def convert_messages(self, messages: list[dict[str, Any]], **_kwargs) -> list[dict[str, Any]]:
        sanitized = []
        changed = False
        for message in messages:
            clean = {key: value for key, value in message.items() if key not in _INTERNAL_MESSAGE_KEYS}
            tool_calls = clean.get("tool_calls")
            if tool_calls:
                clean["tool_calls"] = [
                    {key: value for key, value in call.items() if key in {"id", "type", "function"}}
                    for call in tool_calls
                ]
            changed = changed or clean != message
            sanitized.append(clean)
        return sanitized if changed else messages

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def build_kwargs(
        self, model: str, messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None, **params,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self.convert_messages(messages),
        }
        if tools:
            kwargs["tools"] = self.convert_tools(tools)
            kwargs["tool_choice"] = "auto"
        if params.get("max_tokens") is not None:
            kwargs["max_tokens"] = params["max_tokens"]
        if params.get("timeout") is not None:
            kwargs["timeout"] = params["timeout"]
        overrides = params.get("request_overrides") or {}
        kwargs.update(overrides)
        return kwargs

    def normalize_response(self, response: Any, **_kwargs) -> NormalizedResponse:
        choice = response.choices[0]
        message = choice.message
        tool_calls = None
        if getattr(message, "tool_calls", None):
            tool_calls = []
            for call in message.tool_calls:
                function = getattr(call, "function", None)
                if function is None or not getattr(function, "name", None):
                    continue
                tool_calls.append(ToolCall(
                    id=getattr(call, "id", None),
                    name=function.name,
                    arguments=getattr(function, "arguments", None) or "{}",
                ))
        finish_reason = getattr(choice, "finish_reason", None) or "stop"
        if isinstance(finish_reason, str):
            finish_reason = {
                "STOP": "stop", "MAX_TOKENS": "length",
                "tool_use": "tool_calls", "function_call": "tool_calls",
            }.get(finish_reason, finish_reason.lower())
        usage = Usage.from_openai(response.usage) if getattr(response, "usage", None) else None
        reasoning_content = getattr(message, "reasoning_content", None)
        provider_data = {"reasoning_content": reasoning_content} if reasoning_content is not None else None
        content = getattr(message, "content", None)
        refusal = getattr(message, "refusal", None)
        if refusal and not content and not tool_calls:
            content = refusal
            finish_reason = "content_filter"
        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=getattr(message, "reasoning", None),
            usage=usage,
            provider_data=provider_data,
        )

    def validate_response(self, response: Any) -> bool:
        return bool(response is not None and getattr(response, "choices", None))

    def extract_cache_stats(self, response: Any) -> dict[str, int] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
        written = getattr(details, "cache_write_tokens", 0) or 0 if details else 0
        cached = cached or getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        return {"cached_tokens": cached, "creation_tokens": written} if cached or written else None


from agent.transports import register_transport

register_transport("chat_completions", ChatCompletionsTransport)
