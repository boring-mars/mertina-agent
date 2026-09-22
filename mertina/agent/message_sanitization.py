# Ported from hermes-agent agent/message_sanitization.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Message and tool-payload sanitization helpers (pure; documented in-place mutation).

Walk OpenAI-format message lists and structured payloads, repairing or stripping
characters that would crash ``json.dumps`` in the OpenAI SDK or be rejected upstream.
``run_agent`` re-exports them for old imports.
"""

from __future__ import annotations

import re
from typing import Any

# Lone surrogates are invalid UTF-8 and crash json.dumps in the OpenAI SDK; also used for
# CLI paste scrubbing.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD; no-op when none present."""
    return _SURROGATE_RE.sub("\ufffd", text)


def close_interrupted_tool_sequence(
    messages: list[dict[str, Any]], final_response: Any = None
) -> bool:
    """Append a synthetic assistant turn when an interrupted tail is a tool result: a transcript
    ending on a raw ``tool`` message makes the next user message land as ``tool → user``, an
    alternation violation strict providers (Gemini, Claude) answer by hallucinating a
    continuation. Mutates in place; True if a closing turn was appended."""
    last = messages[-1] if messages else None
    if not isinstance(last, dict) or last.get("role") != "tool":
        return False
    text = final_response if isinstance(final_response, str) else ""
    from mertina.agent.message_metadata import append_message

    append_message(
        messages, {"role": "assistant", "content": text.strip() or "Operation interrupted."}
    )
    return True


def _tc_field(tc: Any, key: str) -> Any:
    """Read ``key`` from a tool-call entry that may be a dict or an SDK object."""
    return tc.get(key) if isinstance(tc, dict) else getattr(tc, key, None)


def coalesce_tool_call_id(tc: Any) -> str:
    """Effective call id of a tool_call entry (dict or object); ``""`` when none. Codex Responses
    carry ``call_id`` (authoritative pairing key), Chat Completions ``id`` only, and bridge ids
    may be ``call_id|response_item_id``."""
    for raw in (_tc_field(tc, "call_id"), _tc_field(tc, "id")):
        value = raw.strip() if isinstance(raw, str) else ""
        if value:
            return value.split("|", 1)[0].strip() or value
    return ""


# --------------------------------------------------------------------------- reasoning_content policy —
# single owner (audit F4) --------------------------------------------------------------------------- The
# strip-vs-repad decision was previously forked across the wire files in separate incident commits
# (2b3a4f0af8 strip for strict providers, b5495db701 re-pad for require-side, 94b3131be7/9a9f8a6d99 kimi
# pad). The POLICY — which provider direction gets which treatment — lives here as one rule table + apply
# functions; adapters keep only SYNTAX mapping (e.g. anthropic_adapter turning reasoning_content into a
# thinking block). Direction table: require-side (echo-back enforced; replays 400 without the field): kimi
# — provider kimi-coding/kimi-coding-cn, or host api.kimi.com / moonshot.ai / moonshot.cn. Host-driven on
# purpose: aggregators re-exporting kimi models reject the echo. deepseek — provider "deepseek", model
# contains "deepseek", or host api.deepseek.com (#15250; V4 rejects empty-string pads, hence the " "
# single-space pad, #17341). mimo     — provider "xiaomi", model contains "mimo", or host *.xiaomimimo.com.
# strict side (field rejected with 400/422 "Extra inputs are not permitted"): everyone else — Mistral,
# Cerebras, Groq, SambaNova, … (#45655). Strip the key entirely, even a single-space pad.
_REASONING_ECHO_RULES: tuple = (
    # (family, exact providers (raw), exact providers (lowered), model substrings (lowered), hosts)
    (
        "kimi",
        frozenset({"kimi-coding", "kimi-coding-cn"}),
        frozenset(),
        (),
        ("api.kimi.com", "moonshot.ai", "moonshot.cn"),
    ),
    ("deepseek", frozenset(), frozenset({"deepseek"}), ("deepseek",), ("api.deepseek.com",)),
    (
        "mimo",
        frozenset(),
        frozenset({"xiaomi"}),
        ("mimo",),
        ("api.xiaomimimo.com", "xiaomimimo.com"),
    ),
)


_REASONING_ECHO_RULE_BY_FAMILY = {rule[0]: rule for rule in _REASONING_ECHO_RULES}


def matches_reasoning_echo_family(family: str, provider: Any, model: Any, base_url: Any) -> bool:
    """True when (provider, model, base_url) matches one echo-back family (families can overlap;
    membership is tested independently). Raises KeyError for an unknown family."""
    from mertina.utils import base_url_host_matches

    _, raw_providers, lowered_providers, model_subs, hosts = _REASONING_ECHO_RULE_BY_FAMILY[family]
    model_lower = (model or "").lower()
    return (
        provider in raw_providers
        or (provider or "").lower() in lowered_providers
        or any(sub in model_lower for sub in model_subs)
        or any(base_url_host_matches(base_url, host) for host in hosts)
    )


def apply_reasoning_content_policy(
    source_msg: dict, api_msg: dict, needs_thinking_pad: bool
) -> None:
    """Copy provider-facing reasoning fields onto an API replay message (mutates ``api_msg``).
    ``needs_thinking_pad`` is the require-side flag (``needs_reasoning_echo``)."""
    if source_msg.get("role") != "assistant":
        return
    if not needs_thinking_pad:
        # Strict side: never carry the field — a reasoning primary pads history with " ",
        # then a fallback to Mistral/Cerebras/Groq replays the pad and 422s. Also drops a
        # non-string value (None after compaction): never pass null to the API.
        api_msg.pop("reasoning_content", None)
        return
    existing, reasoning = source_msg.get("reasoning_content"), source_msg.get("reasoning")
    # 1. Explicit reasoning_content already set. When the active provider enforces the thinking-mode
    #   echo-back (DeepSeek / Kimi / MiMo), preserve it verbatim — that includes their own space-placeholder
    #   written at creation time and any valid reasoning from the same provider. Sessions persisted BEFORE
    #   #17341 have empty-string placeholders pinned at creation time; DeepSeek V4 Pro rejects those with
    #   HTTP 400, so upgrade "" → " " on replay. When the active provider does NOT enforce echo-back, strip
    #   the field entirely. Strict OpenAI-compatible providers (Mistral, Cerebras, Groq, SambaNova, …)
    #   reject ANY reasoning_content key in input messages with HTTP 400/422 ("Extra inputs are not
    #   permitted"), even an empty string or a single-space pad. Stripping here covers the rebuild path;
    #   ``reapply_reasoning_echo`` covers the already-built api_messages path. Refs #45655.
    if isinstance(existing, str):
        # Explicit value: preserve verbatim, upgrading legacy "" to " " (DeepSeek V4 400s on "").
        api_msg["reasoning_content"] = existing or " "
    elif isinstance(reasoning, str) and reasoning and not source_msg.get("tool_calls"):
        # Healthy session: promote internal 'reasoning' → 'reasoning_content'.
        api_msg["reasoning_content"] = reasoning
    else:
        # tool_calls + 'reasoning' but no 'reasoning_content' means the reasoning came from
        # ANOTHER provider (DeepSeek's own build pins reasoning_content for tool-call turns):
        # pad without leaking foreign CoT. No reasoning at all: every assistant turn still needs
        # the field; " " (not "") because DeepSeek V4 rejects empty string.
        api_msg["reasoning_content"] = " "
