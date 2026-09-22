# Ported from hermes-agent agent/chat_completion_helpers.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""API-call helpers extracted from :class:`AIAgent`: the non-streaming request driver, request
kwargs builder, assistant-message materializer and max-iterations handler.

Each function takes the parent ``AIAgent`` as ``agent``; AIAgent keeps thin forwarders.
"""

from __future__ import annotations

import logging
import re

from mertina.agent.message_content import flatten_message_text
from mertina.agent.message_metadata import append_message, stamp_message_timestamp
from mertina.agent.message_sanitization import (
    _sanitize_surrogates,
)

logger = logging.getLogger(__name__)


def _dispatch_nonstreaming_api_request(agent, api_kwargs: dict):
    """Run one non-streaming LLM request on the shared client and return it."""
    return agent.client.chat.completions.create(**api_kwargs)


def direct_api_call(agent, api_kwargs: dict):
    """Run a non-streaming LLM call inline on the conversation thread."""
    response = _dispatch_nonstreaming_api_request(agent, api_kwargs)
    return response


def interruptible_api_call(agent, api_kwargs: dict):
    """Run the API call inline (see ``direct_api_call``)."""
    return direct_api_call(agent, api_kwargs)


def _build_chat_completions_kwargs(agent, api_messages, tools_for_api):
    transport = agent._get_transport()

    _common = dict(
        model=agent.model,
        messages=api_messages,
        tools=tools_for_api,
        max_tokens=agent.max_tokens,
        max_tokens_param_fn=agent._max_tokens_param,
    )

    return transport.build_kwargs(
        **_common,
    )


def build_api_kwargs(agent, api_messages: list, tools_for_api: list | None = None) -> dict:
    """Build the keyword arguments dict for the chat-completions API."""
    kwargs = _build_api_kwargs_for_mode(agent, api_messages, tools_for_api)
    return kwargs


def _build_api_kwargs_for_mode(
    agent, api_messages: list, tools_for_api: list | None = None
) -> dict:
    if tools_for_api is None:
        tools_for_api = agent.tools
    builder = _build_chat_completions_kwargs
    return builder(agent, api_messages, tools_for_api)


def _model_dump_safe(obj):
    """``model_dump(warnings=False)`` (avoids pydantic serializer UserWarnings on
    generic-union SDK models), falling back for shims that reject the kwarg."""
    try:
        return obj.model_dump(warnings=False)
    except TypeError:
        return obj.model_dump()


def _dump_if_model(value):
    return _model_dump_safe(value) if hasattr(value, "model_dump") else value


def _assistant_reasoning_text(agent, assistant_message) -> str | None:
    """Structured reasoning, else inline ``<think>`` blocks embedded in content."""
    reasoning_text = agent._extract_reasoning(assistant_message)
    if not reasoning_text:
        content = flatten_message_text(getattr(assistant_message, "content", None))
        think_blocks = re.findall(r"<think>(.*?)</think>", content, flags=re.DOTALL)
        if think_blocks:
            reasoning_text = "\n\n".join(b.strip() for b in think_blocks if b.strip()) or None
    if reasoning_text and agent.verbose_logging:
        logging.debug(f"Captured reasoning ({len(reasoning_text)} chars): {reasoning_text}")
    return _sanitize_surrogates(reasoning_text) if reasoning_text else reasoning_text


def _assistant_content_for_storage(agent, assistant_message):
    # Sanitize surrogates (Kimi/GLM via Ollama emit code points that crash json.dumps), then
    # strip inline <think> tags at the storage boundary (they leaked to platforms and
    # polluted titles).
    content = _sanitize_surrogates(
        flatten_message_text(getattr(assistant_message, "content", None))
    )
    if isinstance(content, str) and content:
        content = agent._strip_think_blocks(content).strip()
    return content


def _assistant_tool_call_dict(agent, tool_call, index: int) -> dict:
    raw_id = getattr(tool_call, "id", None)
    call_id = getattr(tool_call, "call_id", None)
    if not isinstance(call_id, str) or not call_id.strip():
        if isinstance(raw_id, str) and raw_id.strip():
            call_id = raw_id.strip()
    call_id = call_id.strip()

    # Arguments are deliberately NOT redacted: this dict is replayed to the model every
    # turn, so a ``***`` mask would break credential-dependent commands (#43083).
    tc_dict = {
        "id": call_id,
        "type": tool_call.type,
        "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments},
    }
    # Preserve extra_content (Gemini thought_signature) or Gemini 3 thinking
    # models 400 on the next request.
    extra = getattr(tool_call, "extra_content", None)
    if extra is not None:
        tc_dict["extra_content"] = _dump_if_model(extra)
    return tc_dict


def build_assistant_message(agent, assistant_message, finish_reason: str) -> dict:
    """Build a normalized assistant message dict (reasoning, reasoning_details,
    optional tool_calls) shared by the tool-call and final-response paths.
    Textless turns are NOT padded here."""
    assistant_tool_calls = getattr(assistant_message, "tool_calls", None)
    reasoning_text = _assistant_reasoning_text(agent, assistant_message)
    msg = stamp_message_timestamp(
        {
            "role": "assistant",
            "content": _assistant_content_for_storage(agent, assistant_message),
            "reasoning": reasoning_text,
            "finish_reason": finish_reason,
        }
    )

    raw_reasoning_content = getattr(assistant_message, "reasoning_content", None)
    if raw_reasoning_content is None:
        model_extra = getattr(assistant_message, "model_extra", None) or {}
        if isinstance(model_extra, dict) and "reasoning_content" in model_extra:
            raw_reasoning_content = model_extra["reasoning_content"]
    if raw_reasoning_content is not None:
        msg["reasoning_content"] = _sanitize_surrogates(raw_reasoning_content)
    elif reasoning_text:
        # Streaming-only providers accumulate reasoning via deltas and never set
        # it on the message; replaying through a thinking model then 400s.
        # Promote ONLY when nothing set the field: SDK reasoning_content wins, and
        # reasoning-less turns leave the field absent.
        msg["reasoning_content"] = reasoning_text

    if getattr(assistant_message, "reasoning_details", None):
        # Preserve reasoning_details exactly (opaque signature /
        # encrypted_content fields) for cross-turn reasoning continuity.
        preserved = []
        for d in assistant_message.reasoning_details:
            if isinstance(d, dict):
                preserved.append(d)
            elif hasattr(d, "__dict__"):
                preserved.append(d.__dict__)
            elif hasattr(d, "model_dump"):
                preserved.append(_model_dump_safe(d))
        if preserved:
            msg["reasoning_details"] = preserved

    if assistant_tool_calls:
        msg["tool_calls"] = [
            _assistant_tool_call_dict(agent, tc, i) for i, tc in enumerate(assistant_tool_calls)
        ]
    return msg


# Keys outside the Chat Completions schema that strict gateways (Fireworks-backed OpenCode
# Go, Mistral, Moonshot/Kimi) reject with 422. The transport's convert_messages() drops them
# in the main loop; the summary path calls chat.completions.create() directly, so mirror it.
_SUMMARY_FOREIGN_MESSAGE_KEYS = (
    "reasoning",
    "finish_reason",
    "tool_name",
    "codex_reasoning_items",
    "codex_message_items",
    "timestamp",
    "platform_message_id",
)


_EMPTY_SUMMARY_RESPONSE = "I reached the iteration limit and couldn't generate a summary."


def _iteration_summary_api_messages(agent, messages: list) -> list:
    """Wire-ready messages for the summary call, mirroring the main loop's api_messages build
    (reasoning replay, schema-foreign key strip, underscore-key sweep)."""
    api_messages = []
    for msg in messages:
        api_msg = msg.copy()
        agent._copy_reasoning_content_for_api(msg, api_msg)
        for key in _SUMMARY_FOREIGN_MESSAGE_KEYS:
            api_msg.pop(key, None)
        # Mirror of the transport's role-qualified strip: ``name`` is
        # schema-foreign on tool results only (strict providers reject with
        # "contains item with unknown key name"); it stays on user/assistant.
        if api_msg.get("role") == "tool":
            api_msg.pop("name", None)
        api_messages.append(api_msg)

    effective_system = agent._cached_system_prompt or ""
    if effective_system:
        api_messages = [{"role": "system", "content": effective_system}] + api_messages

    for api_msg in api_messages:  # underscore scaffolding: the transport's sweeper is bypassed here
        if isinstance(api_msg, dict):
            for internal_key in [k for k in api_msg if isinstance(k, str) and k.startswith("_")]:
                del api_msg[internal_key]
    return api_messages


def _summary_text(agent, response, **normalize_kwargs) -> str:
    normalized = agent._get_transport().normalize_response(response, **normalize_kwargs)
    if normalized.tool_calls:
        # No summary path executes tool calls; log so a tool-only response that falls into the
        # empty-summary retry is diagnosable.
        logger.warning("Iteration summary emitted tool calls; discarding them")
    return (normalized.content or "").strip()


def _chat_summary_attempt(agent, api_messages: list):
    # Same kwargs builder as the main loop so the summary keeps the cached prefix. Do not omit
    # tools or force tool_choice="none" here: SGLang renders the prompt with tools=None in that
    # mode and the KV prefix diverges.
    summary_kwargs = agent._build_api_kwargs(api_messages)

    def _attempt(retry_count: int) -> str:
        summary_client = agent._ensure_primary_openai_client(
            reason="iteration_limit_summary_retry" if retry_count else "iteration_limit_summary"
        )
        response = summary_client.chat.completions.create(**summary_kwargs)
        return _summary_text(agent, response)

    return _attempt


def handle_max_iterations(agent, messages: list, api_call_count: int) -> str:
    """Request a summary when max iterations are reached. Returns the final response text."""
    warning = f"⚠️  Reached maximum iterations ({agent.max_iterations}). Requesting summary..."
    if getattr(agent, "suppress_status_output", False):
        # Strict machine-readable mode (-Q, oneshot): keep diagnostics off stdout. quiet_mode is
        # NOT the gate — the interactive CLI runs quiet_mode=True by default and must see this.
        logger.warning(warning)
    else:
        agent._safe_print(warning, diagnostic=True)

    # Shared constant so compaction recognizers can identify this runtime nudge by its stable
    # content after SessionDB projection strips metadata flags.
    from mertina.agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST

    append_message(messages, {"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST})

    try:
        api_messages = _iteration_summary_api_messages(agent, messages)
        build_attempt = _chat_summary_attempt
        attempt = build_attempt(agent, api_messages)

        # One retry on an empty summary; a summary empty once its <think> block is stripped is NOT retried.
        final_response = _EMPTY_SUMMARY_RESPONSE
        for retry_count in (0, 1):
            text = attempt(retry_count)
            if not text:
                continue
            if "<think>" in text:
                text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
            if text:
                append_message(messages, {"role": "assistant", "content": text})
                final_response = text
            break

    except Exception as e:
        logger.warning("Failed to get summary response: %s", e)
        from mertina.agent.turn_failure_copy import site_copy

        final_response = site_copy("max_iterations_no_summary", limit=agent.max_iterations)

    return final_response
