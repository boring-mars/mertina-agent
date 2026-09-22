# Ported from hermes-agent agent/usage_pricing.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from typing import Any

logger = logging.getLogger(__name__)


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

    def __add__(self, other: CanonicalUsage) -> CanonicalUsage:
        """Sum two usage buckets. ``raw_usage`` (single-response detail) is
        dropped; ``request_count`` adds so callers see how many API calls a
        combined figure covers."""
        if not isinstance(other, CanonicalUsage):
            return NotImplemented
        return CanonicalUsage(
            **{
                f.name: getattr(self, f.name) + getattr(other, f.name)
                for f in fields(CanonicalUsage)
                if f.name != "raw_usage"
            }
        )


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


# Usage-field candidate paths per API shape: (input/prompt total, output, cache
# read, cache write); the first non-zero path wins.
_ANTHROPIC_USAGE_SHAPE = (
    (("input_tokens",),),
    (("output_tokens",),),
    (("cache_read_input_tokens",),),
    (("cache_creation_input_tokens",),),
)


# OpenAI's documented GPT-5.6+ field is `cache_write_tokens` (billed at 1.25x);
# `cache_creation_tokens` is a fallback for older endpoints.
_CODEX_USAGE_SHAPE = (
    (("input_tokens",),),
    (("output_tokens",),),
    (("input_tokens_details", "cached_tokens"),),
    (
        ("input_tokens_details", "cache_write_tokens"),
        ("input_tokens_details", "cache_creation_tokens"),
    ),
)


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
    response_usage: Any, *, provider: str | None = None, api_mode: str | None = None
) -> CanonicalUsage:
    """Normalize raw API response usage into canonical token buckets (Anthropic,
    Codex Responses, or OpenAI Chat Completions shape)."""
    if not response_usage:
        return CanonicalUsage()

    provider_name = (provider or "").strip().lower()
    mode = (api_mode or "").strip().lower()
    u = response_usage

    if mode == "anthropic_messages" or provider_name == "anthropic":
        shape = _ANTHROPIC_USAGE_SHAPE
    elif mode == "codex_responses":
        shape = _CODEX_USAGE_SHAPE
    else:
        shape = _CHAT_USAGE_SHAPE
    prompt_total, output_tokens, cache_read_tokens, cache_write_tokens = (
        _first_nonzero(u, *paths) for paths in shape
    )
    # Anthropic reports uncached input directly; Codex/Chat totals INCLUDE
    # cached tokens, so the cache buckets are subtracted back out.
    input_tokens = (
        prompt_total
        if shape is _ANTHROPIC_USAGE_SHAPE
        else max(0, prompt_total - cache_read_tokens - cache_write_tokens)
    )

    # Responses API: output_tokens_details.reasoning_tokens. Chat Completions
    # (OpenAI, OpenRouter, DeepSeek, ...): completion_tokens_details.reasoning_tokens.
    # Hidden thinking dominates output spend on reasoning models, so read both.
    reasoning_tokens = _first_nonzero(
        u,
        ("output_tokens_details", "reasoning_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
    )

    # On MiniMax-M3's Anthropic wire, cache_read_input_tokens carries a constant
    # +128 floor and cache_creation is always 0, so cache_read is not a reliable
    # hit signal; the input_tokens drop between consecutive calls is.
    # Docs: https://platform.minimax.io/docs/api-reference/text-prompt-caching
    if provider_name in {"minimax", "minimax-cn"} and mode == "anthropic_messages":
        logger.debug(
            "cache_observability provider=%s mode=%s input_tokens=%s "
            "output_tokens=%s cache_read_tokens=%s cache_write_tokens=%s "
            "(note: on MiniMax-M3 cache_read carries a +128 constant "
            "floor and is not a reliable hit signal — track input_tokens "
            "drops across calls instead)",
            provider_name,
            mode,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
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
