# Ported from hermes-agent agent/transports/chat_completions.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""OpenAI Chat Completions transport (default api_mode for OpenAI-compatible providers).

Messages/tools are already OpenAI-shaped, so convert_* are near-identity; the
provider-specific work lives in build_kwargs (max_tokens).
"""

from typing import Any

from mertina.agent.transports.base import ProviderTransport
from mertina.agent.transports.types import NormalizedResponse, ToolCall, Usage

# Persistence-only / cross-transport message keys that strict OpenAI-compatible
# providers reject with HTTP 400 ("Extra inputs are not permitted").
_STRIP_MSG_KEYS = (
    "tool_name",
    "effect_disposition",
    "timestamp",
    "platform_message_id",
    "api_content",
)


def _model_consumes_thought_signature(model: Any) -> bool:
    """True for Gemini-family targets, which require tool-call ``extra_content``
    (thought_signature) replay.

    Every other strict provider rejects it, so it is stripped for non-Gemini targets.
    """
    m = str(model or "").lower()
    return "gemini" in m or "gemma" in m


def _has_replayable_thought_signature(extra_content: Any) -> bool:
    """Whether OpenRouter's Gemini sidecar contains a usable thought signature.

    Gemini accepts the signature either directly or under its ``google``
    namespace.  Replaying an empty or non-string value makes a multimodal
    request fail with ``Corrupted thought signature``; omit that sidecar while
    leaving the stored history untouched.
    """
    if not isinstance(extra_content, dict):
        return False
    candidate = extra_content.get("thought_signature")
    google = extra_content.get("google")
    if candidate is None and isinstance(google, dict):
        candidate = google.get("thought_signature")
    return isinstance(candidate, str) and bool(candidate.strip())


def _attr_or_model_extra(obj: Any, name: str) -> Any:
    """``obj.<name>``, else the same key from pydantic ``model_extra`` (some SDKs park fields
    there)."""
    value = getattr(obj, name, None)
    if value is None and hasattr(obj, "model_extra"):
        value = (obj.model_extra if isinstance(obj.model_extra, dict) else {}).get(name)
    return value


def _dump_extra_content(extra: Any) -> Any:
    """Plain-dict form of a pydantic ``extra_content``; older pydantic lacks ``warnings=``, so
    retry without it."""
    if hasattr(extra, "model_dump"):
        for dump_kwargs in ({"warnings": False}, {}):
            try:
                return extra.model_dump(**dump_kwargs)
            except TypeError:
                continue
            except Exception:
                break
    return extra


def _apply_max_tokens(api_kwargs: dict[str, Any], params: dict[str, Any]) -> None:
    """Preserve provider protocol exceptions."""
    max_tokens_fn = params.get("max_tokens_param_fn")
    candidate = params.get("max_tokens")
    if candidate is not None and max_tokens_fn:
        api_kwargs.update(max_tokens_fn(candidate))


def _base_kwargs(
    model: str, sanitized: list[dict[str, Any]], tools: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Shared ``{model, messages[, timeout][, tools]}`` scaffold."""
    api_kwargs: dict[str, Any] = {"model": model, "messages": sanitized}
    if params.get("timeout") is not None:
        api_kwargs["timeout"] = params["timeout"]
    if tools:
        api_kwargs["tools"] = tools
    return api_kwargs


def _sanitize_message(
    msg: Any,
    strip_extra_content: bool,
    strip_reasoning_details: bool = False,
) -> dict[str, Any] | None:
    """Sanitized copy of ``msg``, or None when nothing needs stripping.

    Drops persistence sidecars, ``_``-prefixed scaffolding markers, tool-call
    ``extra_content`` unless Gemini, an assistant
    ``tool_calls: []`` / ``null`` (strict providers reject both), ``name``
    on tool results (schema-valid only on user/assistant messages; strict
    providers reject it with ``contains item with unknown key name``), and
    ``reasoning_details``.
    """
    if not isinstance(msg, dict):
        return None
    strip_keys = [
        k for k in msg if k in _STRIP_MSG_KEYS or (isinstance(k, str) and k.startswith("_"))
    ]
    if strip_reasoning_details and "reasoning_details" in msg:
        strip_keys.append("reasoning_details")
    # ``name`` is schema-valid on user/assistant messages, so the removal is
    # role-qualified: only tool results carry it illegally (strict providers
    # reject with "contains item with unknown key name").
    if msg.get("role") == "tool" and "name" in msg:
        strip_keys.append("name")
    out_msg = {k: v for k, v in msg.items() if k not in strip_keys}
    tool_calls = msg.get("tool_calls")
    copied_tool_calls = None
    if (
        msg.get("role") == "assistant"
        and "tool_calls" in msg
        and (tool_calls is None or (isinstance(tool_calls, list) and not tool_calls))
    ):
        out_msg.pop("tool_calls", None)
        strip_keys.append("tool_calls")
    elif isinstance(tool_calls, list):
        for tc_idx, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                continue
            keys: list[str] = []
            if "extra_content" in tc and (
                strip_extra_content or not _has_replayable_thought_signature(tc["extra_content"])
            ):
                keys.append("extra_content")
            if keys:
                if copied_tool_calls is None:
                    copied_tool_calls = list(tool_calls)
                copied_tool_calls[tc_idx] = {k: v for k, v in tc.items() if k not in keys}
        if copied_tool_calls is not None:
            out_msg["tool_calls"] = copied_tool_calls
    return out_msg if strip_keys or copied_tool_calls is not None else None


class ChatCompletionsTransport(ProviderTransport):
    """Transport for api_mode='chat_completions'."""

    @property
    def api_mode(self) -> str:
        return "chat_completions"

    def convert_messages(
        self, messages: list[dict[str, Any]], **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Strip internal fields that strict chat-completions providers reject (HTTP 400/422).

        Returns the input list unchanged when nothing needs sanitizing.
        """
        strip_extra_content = not _model_consumes_thought_signature(kwargs.get("model"))
        sanitized_pairs = [
            (m, _sanitize_message(m, strip_extra_content, strip_reasoning_details=True))
            for m in messages
        ]
        if all(s is None for _, s in sanitized_pairs):
            return messages
        return [m if s is None else s for m, s in sanitized_pairs]

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Tools are already in OpenAI format — identity."""
        return tools

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Build chat.completions.create() kwargs."""
        sanitized = self.convert_messages(messages, model=model)
        api_kwargs = _base_kwargs(model, sanitized, tools, params)
        _apply_max_tokens(api_kwargs, params)
        return api_kwargs

    def normalize_response(self, response: Any, **kwargs: Any) -> NormalizedResponse:
        """Normalize an OpenAI ChatCompletion.

        Gemini ``extra_content`` rides on ToolCall.provider_data; ``reasoning_content`` and
        ``reasoning_details`` stay distinct in provider_data because downstream reads them so.
        """
        choice = response.choices[0]
        msg: Any = getattr(choice, "message", None)
        finish_reason = getattr(choice, "finish_reason", None) or "stop"

        tool_calls = None
        if getattr(msg, "tool_calls", None):
            tool_calls = [
                tc
                for tc in (self._normalize_tool_call(tc) for tc in msg.tool_calls)
                if tc is not None
            ]

        usage = (
            Usage.from_openai(response.usage)
            if hasattr(response, "usage") and response.usage
            else None
        )

        # Fields some SDKs park in pydantic ``model_extra`` rather than as attributes.
        reasoning_content = _attr_or_model_extra(msg, "reasoning_content")
        provider_data: dict[str, Any] = {}
        if reasoning_content is not None:
            provider_data["reasoning_content"] = reasoning_content
        if getattr(msg, "reasoning_details", None):
            provider_data["reasoning_details"] = msg.reasoning_details

        # OpenAI structured refusal (``message.refusal`` set, ``content`` empty); without
        # promotion the loop retries a deterministic refusal as an empty response.
        content = getattr(msg, "content", None)
        refusal = _attr_or_model_extra(msg, "refusal")
        if isinstance(refusal, str) and refusal.strip():
            provider_data["refusal"] = refusal
            # Terminal ``content_filter`` only when the refusal is the sole payload.
            if not (isinstance(content, str) and content.strip()) and not tool_calls:
                content = refusal
                if finish_reason in (None, "stop"):
                    finish_reason = "content_filter"

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=getattr(msg, "reasoning", None),
            usage=usage,
            provider_data=provider_data or None,
        )

    def _normalize_tool_call(self, tc: Any) -> ToolCall | None:
        """One SDK tool call -> ToolCall; None when it lacks a function/name (matches Relay's
        codec)."""
        tc_function = getattr(tc, "function", None)
        name = getattr(tc_function, "name", None)
        if tc_function is None or name is None:
            return None
        arguments = getattr(tc_function, "arguments", None)
        extra = _attr_or_model_extra(tc, "extra_content")
        return ToolCall(
            id=getattr(tc, "id", None),
            name=name,
            arguments="{}" if arguments is None else arguments,
            provider_data=None if extra is None else {"extra_content": _dump_extra_content(extra)},
        )

    def validate_response(self, response: Any) -> bool:
        """Check that response has valid choices."""
        if response is None or not getattr(response, "choices", None):
            return False
        return True

    def extract_cache_stats(self, response: Any) -> dict[str, int] | None:
        """Cache stats from prompt_tokens_details (OpenRouter/OpenAI) or DeepSeek's top-level
        prompt_cache_hit_tokens."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
        written = getattr(details, "cache_write_tokens", 0) or 0 if details else 0
        cached = cached or getattr(usage, "prompt_cache_hit_tokens", 0) or 0  # DeepSeek native
        return {"cached_tokens": cached, "creation_tokens": written} if cached or written else None


from mertina.agent.transports import register_transport  # noqa: E402

register_transport("chat_completions", ChatCompletionsTransport)
