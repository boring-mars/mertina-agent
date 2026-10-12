"""Per-response usage accounting for the conversation turn loop.

After every successful model API call, ``record_response_usage`` folds ``response.usage``
into: the context compressor (``update_from_response`` + the compression-budget rearm
latch), the usage anchor for display/compression math, per-session token/cost counters,
the state.db token-delta queue, and the observability log line. MoA sessions additionally
fold advisor fan-out usage into the reported counts and price the aggregator at its REAL
model/provider. Logger name stays ``agent.conversation_loop`` for caplog parity.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Dict, List

from agent.usage_anchor import capture_usage_anchor, set_usage_anchor
from agent.usage_pricing import normalize_usage

logger = logging.getLogger("agent.conversation_loop")




@dataclass
class ResponseUsageOutcome:
    """``compression_attempts`` is the (possibly rearmed-to-zero) budget counter;
    ``rearmed`` tells the loop to also clear its preflight-block latch."""

    compression_attempts: int
    rearmed: bool = False






def record_response_usage(
    agent: Any, response: Any, *, messages: List[Dict[str, Any]], api_call_count: int,
    api_duration: float, compression_attempts: int, max_compression_attempts: int,
) -> ResponseUsageOutcome:
    """Fold ``response.usage`` into compressor, anchors, session counters, state.db
    and the API-call log line (see module docstring). No-usage responses only
    consume a pending compaction verdict. Returns the loop-visible outcome."""
    rearmed = False
    # Count every completed provider attempt, including providers that omit usage.
    # Token/cost accounting below stays gated on real usage, but the request itself
    # must remain observable.
    agent.session_api_calls += 1
    if not (hasattr(response, 'usage') and response.usage):
        logger.info(
            "API call #%d: model=%s provider=%s in=? out=? total=? latency=%.1fs usage=unavailable",
            agent.session_api_calls, agent.model, agent.provider or "unknown", api_duration,
        )
        return ResponseUsageOutcome(compression_attempts=compression_attempts, rearmed=rearmed)

    canonical_usage = normalize_usage(response.usage, provider=agent.provider, api_mode=agent.api_mode)
    prompt_tokens = canonical_usage.prompt_tokens
    completion_tokens = canonical_usage.output_tokens
    total_tokens = canonical_usage.total_tokens
    # Canonical token + cache buckets for context engines; legacy keys stay for back-compat.
    usage_dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_tokens": canonical_usage.input_tokens,
        "output_tokens": canonical_usage.output_tokens,
        "cache_read_tokens": canonical_usage.cache_read_tokens,
        "cache_write_tokens": canonical_usage.cache_write_tokens,
        "reasoning_tokens": canonical_usage.reasoning_tokens,
    }
    # Stash canonical usage for on_turn_complete(); keep the latest call's.
    agent._last_turn_usage = dict(usage_dict)
    # The parent's CURRENT prompt size for headroom math (delegate summary budgets): the
    # aggregator's own prompt, never the MoA-folded total (advisor prompts are not in this context).
    agent._last_prompt_size_tokens = int(canonical_usage.prompt_tokens or 0)

    agent.session_prompt_tokens += prompt_tokens
    agent.session_completion_tokens += completion_tokens
    agent.session_total_tokens += total_tokens
    agent.session_input_tokens += canonical_usage.input_tokens
    agent.session_output_tokens += canonical_usage.output_tokens
    agent.session_cache_read_tokens += canonical_usage.cache_read_tokens
    agent.session_cache_write_tokens += canonical_usage.cache_write_tokens
    agent.session_reasoning_tokens += canonical_usage.reasoning_tokens
    # Rolling history for status-bar averages (last 10).
    with suppress(Exception):
        hist = getattr(agent, "_api_latency_history", None)
        if hist is not None:
            hist.append(float(api_duration))
        ohist = getattr(agent, "_api_output_history", None)
        if ohist is not None:
            ohist.append(int(canonical_usage.output_tokens or 0))

    _cache_pct = ""
    if canonical_usage.cache_read_tokens and prompt_tokens:
        _cache_pct = f" cache={canonical_usage.cache_read_tokens}/{prompt_tokens} ({100*canonical_usage.cache_read_tokens/prompt_tokens:.0f}%)"
    # write= is the money (cache writes cost 50x a read); id= is what a provider needs to look the
    # request up; upstream= is who actually served it when the route reports that (OpenRouter's
    # `provider`). Diagnosing the 1,393-agent run's cache misses took a DB join and a live probe
    # because none of the three were on this line.
    if canonical_usage.cache_write_tokens:
        _cache_pct += f" write={canonical_usage.cache_write_tokens}"
    _rid = getattr(response, "id", None)
    _ident = f" id={_rid}" if isinstance(_rid, str) and _rid else ""
    _upstream = getattr(response, "provider", None)
    if isinstance(_upstream, str) and _upstream:
        _ident += f" upstream={_upstream}"
    logger.info(
        "API call #%d: model=%s provider=%s in=%d out=%d total=%d latency=%.1fs%s%s",
        agent.session_api_calls, agent.model, agent.provider or "unknown",
        prompt_tokens, completion_tokens, total_tokens,
        api_duration, _cache_pct, _ident,
    )
    if agent.verbose_logging:
        logging.debug(f"Token usage: prompt={usage_dict['prompt_tokens']:,}, completion={usage_dict['completion_tokens']:,}, total={usage_dict['total_tokens']:,}")

    # Report cache stats for any provider that returns ``prompt_tokens_details.cached_tokens``,
    # not only when we inject cache_control markers.
    cached = canonical_usage.cache_read_tokens
    written = canonical_usage.cache_write_tokens
    prompt = usage_dict["prompt_tokens"]
    if (cached or written) and not agent.quiet_mode:
        hit_pct = (cached / prompt * 100) if prompt > 0 else 0
        agent._vprint(
            f"{agent.log_prefix}   💾 Cache: "
            f"{cached:,}/{prompt:,} tokens "
            f"({hit_pct:.0f}% hit, {written:,} written)"
        )
    return ResponseUsageOutcome(compression_attempts=compression_attempts, rearmed=rearmed)
