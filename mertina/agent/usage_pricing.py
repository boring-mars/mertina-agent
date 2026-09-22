# Ported from hermes-agent agent/usage_pricing.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CanonicalUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    request_count: int = 1
    raw_usage: dict[str, Any] | None = None

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens


def _usage_field(obj: Any, *path: str) -> int:
    """Non-negative int at ``obj.path[0].path[1]...``; 0 if any hop is falsy or
    non-numeric. Hops read dicts and attribute objects alike (the Responses API
    returns either); negative counters from providers are clamped so they cannot
    corrupt session accounting."""
    for hop in path:
        if not obj:
            return 0
        obj = obj.get(hop, 0) if isinstance(obj, dict) else getattr(obj, hop, 0)
    try:
        return max(0, int(obj or 0))
    except Exception:
        return 0


def _first_nonzero(obj: Any, *paths: tuple[str, ...]) -> int:
    """First non-zero ``_usage_field`` across candidate paths, else 0."""
    return next((v for v in (_usage_field(obj, *path) for path in paths) if v), 0)


# Usage-field candidate paths: (input/prompt total, output, cache read, cache write); the
# first non-zero path wins.
# OpenAI-style names first, then Anthropic-style: local OpenAI-compatible
# servers (e.g. mlx_vlm.server) emit input_tokens/output_tokens and the OpenAI
# client preserves them as extra attributes. Cache reads: nested OpenAI shape,
# then Anthropic-style top-level fields exposed by proxies routing Claude
# (OpenRouter, Vercel AI Gateway, Cline), then DeepSeek's prompt_cache_hit_tokens,
# then Kimi/Moonshot's cached_tokens — without these, direct sessions show 0
# hits and bill hits at the full input rate.
_CHAT_USAGE_SHAPE = (
    (("prompt_tokens",), ("input_tokens",)),
    (("completion_tokens",), ("output_tokens",)),
    (
        ("prompt_tokens_details", "cached_tokens"),
        ("cache_read_input_tokens",),
        ("prompt_cache_hit_tokens",),
        ("cached_tokens",),
    ),
    (
        ("prompt_tokens_details", "cache_write_tokens"),
        ("prompt_tokens_details", "cache_creation_input_tokens"),
        ("cache_creation_input_tokens",),
        ("cache_write_tokens",),
    ),
)


def normalize_usage(
    response_usage: Any,
) -> CanonicalUsage:
    """Normalize raw API response usage into canonical token buckets (OpenAI Chat
    Completions shape)."""
    if not response_usage:
        return CanonicalUsage()

    u = response_usage

    shape = _CHAT_USAGE_SHAPE
    prompt_total, output_tokens, cache_read_tokens, cache_write_tokens = (
        _first_nonzero(u, *paths) for paths in shape
    )
    # Chat totals INCLUDE cached tokens, so the cache buckets are subtracted back out.
    input_tokens = max(0, prompt_total - cache_read_tokens - cache_write_tokens)

    # Responses API: output_tokens_details.reasoning_tokens. Chat Completions
    # (OpenAI, OpenRouter, DeepSeek, ...): completion_tokens_details.reasoning_tokens.
    # Hidden thinking dominates output spend on reasoning models, so read both.
    reasoning_tokens = _first_nonzero(
        u,
        ("output_tokens_details", "reasoning_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
    )

    return CanonicalUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        raw_usage=dict(u)
        if isinstance(u, dict)
        else (u.model_dump() if callable(getattr(u, "model_dump", None)) else None),
    )
