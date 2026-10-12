"""Per-iteration API request assembly for the conversation turn loop: build ``api_messages``
from the transcript, append MoA context, inject prefills, run the context-engine selection
hook and the send-time sanitizers, canonicalize for bit-perfect cache prefixes, build the
request-local prompt-cache plan LAST (after every transcript mutation), prepare the
persistent-MoA request, then measure request pressure. Nothing here imports
``agent.conversation_loop`` at module level (cycle) — loop-internal helpers resolve lazily
so ``patch("agent.conversation_loop.X")`` sites keep intercepting.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from agent.message_sanitization import _sanitize_messages_surrogates
from agent.usage_anchor import anchored_context_tokens
from agent.prompt_caching import build_prompt_cache_plan, effective_cache_ttl
from agent.turn_context import build_api_messages

logger = logging.getLogger("agent.conversation_loop")


@dataclass
class AssembledRequest:
    """Always ``action == "fallthrough"``; the fields are the iteration locals the assembly
    produces (``api_messages``/``tools_for_api`` are the decorated request copies — the
    canonical ``messages``/``agent.tools`` stay undecorated)."""

    action: str
    api_messages: Any
    tools_for_api: Any
    _moa_prepared_request: Any
    pending_moa_prepared_request: Any
    approx_tokens: Any
    request_pressure_tokens: Any
    total_chars: Any


def _append_moa_context(agent: Any, api_messages: Any, moa_config: Any, original_user_message: Any) -> None:
    """Run the MoA reference models and append their aggregated context to the last user
    message (as a trailing text part on multimodal turns). Fail-open."""
    try:
        from agent.message_content import flatten_message_text as _flatten_mt
        from agent.moa_loop import _preset_temperature, aggregate_moa_context

        _moa_context = aggregate_moa_context(
            user_prompt=(
                original_user_message
                if isinstance(original_user_message, str)
                # Multimodal content list: extract visible text rather than
                # str()-ing parts, which would leak base64 image payloads.
                else _flatten_mt(original_user_message)
            ),
            api_messages=api_messages,
            reference_models=moa_config.get("reference_models") or [],
            aggregator=moa_config.get("aggregator") or {},
            temperature=_preset_temperature(moa_config, "reference_temperature"),
            aggregator_temperature=_preset_temperature(moa_config, "aggregator_temperature"),

            # None = no per-preset override; inherit auxiliary.moa_reference.timeout.
            reference_timeout=(
                float(moa_config["reference_timeout"])
                if moa_config.get("reference_timeout")
                else None
            ),
            degraded_reference_policy=str(
                moa_config.get("degraded_reference_policy") or "loud"
            ),
            agent=agent,
        )
        if not _moa_context:
            return
        for _msg in reversed(api_messages):
            if _msg.get("role") == "user":
                _base = _msg.get("content", "")
                if isinstance(_base, str):
                    _msg["content"] = _base + "\n\n" + _moa_context
                elif isinstance(_base, list):
                    _msg["content"] = [*_base, {"type": "text", "text": "\n\n" + _moa_context}]
                break
    except Exception as _moa_exc:
        logger.warning("MoA context aggregation failed: %s", _moa_exc)


def _prepare_moa_request(agent: Any, api_messages: Any, pending_moa_prepared_request: Any) -> tuple:
    """Persistent-MoA request: rebase the pending prepared request onto the new messages
    when the client supports it, else prepare a fresh one. Returns
    ``(prepared_request, api_messages, pending_moa_prepared_request)``."""
    _moa_completions = getattr(getattr(agent.client, "chat", None), "completions", None)
    prepared: Any = None
    if pending_moa_prepared_request is not None:
        _rebase = getattr(_moa_completions, "rebase_prepared_request", None)
        if callable(_rebase):
            prepared = _rebase(pending_moa_prepared_request, api_messages)
        pending_moa_prepared_request = None
    if prepared is None:
        _prepare = getattr(_moa_completions, "prepare", None)
        if callable(_prepare):
            prepared = _prepare(api_messages)
    if prepared is not None:
        api_messages = prepared["messages"]
    return prepared, api_messages, pending_moa_prepared_request


def assemble_api_request(
    agent: Any, *, messages: Any, current_turn_user_idx: Any, _ext_prefetch_cache: Any,
    _plugin_user_context: Any, moa_config: Any, active_system_prompt: Any,
    original_user_message: Any, pending_moa_prepared_request: Any, request_logger: Any,
) -> AssembledRequest:
    """Assemble the request in the original order. ORDER IS LOAD-BEARING: cache breakpoints
    are injected only after whitespace normalization, the orphan sweep, thinking-only drop /
    user merge and surrogate stripping, so the same row's bytes never vary across turns."""
    from agent.conversation_loop import (
        _CODEX_INCOMPLETE_NUDGE, _apply_context_engine_selection, _canonicalize_api_tool_calls,
        _clone_message_for_send, _midturn_request_pressure_tokens, _pressure_with_real_floor,
    )
    from agent.model_metadata import estimate_messages_tokens_rough

    api_messages, effective_system = build_api_messages(
        agent, messages, current_turn_user_idx=current_turn_user_idx,
        ext_prefetch_cache=_ext_prefetch_cache, plugin_user_context=_plugin_user_context,
        moa_config=moa_config, active_system_prompt=active_system_prompt,
    )

    if moa_config:
        _append_moa_context(agent, api_messages, moa_config, original_user_message)

    # Ephemeral prefill messages go right after the system prompt, API-call-time only.
    if agent.prefill_messages:
        sys_offset = 1 if (api_messages and api_messages[0].get("role") == "system") else 0
        for idx, pfm in enumerate(agent.prefill_messages):
            # Structural clone: the in-place sanitizers below must not write
            # through into agent.prefill_messages' nested containers.
            api_messages.insert(sys_offset + idx, _clone_message_for_send(pfm))

    # Normalize whitespace and tool-call JSON for bit-perfect prefixes across turns
    # (KV-cache reuse on local servers, better cloud cache hits); API copy only.
    for am in api_messages:
        if isinstance(am.get("content"), str):
            am["content"] = am["content"].strip()
    _canonicalize_api_tool_calls(api_messages)

    # Strip lone surrogates (U+D800-U+DFFF) that some Ollama-served models emit;
    # they crash json.dumps() inside the OpenAI SDK and trigger the 3-retry cycle.
    _sanitize_messages_surrogates(api_messages)

    # No send-time pad loop here: ``repair_empty_non_final_messages`` (inside
    # ``_sanitize_api_messages``) is the single owner of empty-turn repair.

    tools_for_api = agent.tools

    # Prepare the persistent-MoA request before measuring compression pressure: the
    # ephemeral advisor output is absent from ``messages``; ``create()`` reuses the
    # prepared request instead of running the advisors again.
    _moa_prepared_request = None
    if agent.provider == "moa":
        _moa_prepared_request, api_messages, pending_moa_prepared_request = _prepare_moa_request(
            agent, api_messages, pending_moa_prepared_request
        )

    # One image-stripped estimate feeds both figures; tools counted separately (50+
    # tools ≈ 20-30K tokens); total_chars is a rough proxy for logs/hooks only.
    # Charge stale thinking only when the active route replays it.
    from agent.turn_context import _agent_stale_thinking_on_wire

    if _agent_stale_thinking_on_wire(agent):
        approx_tokens = estimate_messages_tokens_rough(api_messages)
    else:
        approx_tokens = estimate_messages_tokens_rough(api_messages, charge_stale_thinking=False)
    # Route-aware: native Responses compaction prunes the wire payload, so the raw
    # history figure overstates it and fires needless local compression.
    # Route-aware pressure: when the upcoming request is eligible for native Responses compaction the
    # transport will checkpoint-prune the payload before sending — the generic durable-history figure
    # overstates the wire by orders of magnitude on a compacted session and fires a 600s local compression
    # the main request never needed (#96995, mirroring the turn-prologue preflight #96644/#96155).
    request_pressure_tokens = _midturn_request_pressure_tokens(
        agent, api_messages, effective_system or "", approx_tokens
    )
    # Usage-anchored override: real prompt_tokens (incl. system + tool schemas) +
    # delta estimate replaces the whole-history heuristic when the anchor is fresh.
    _anchored_pressure = anchored_context_tokens(messages, getattr(agent, "_usage_anchor", None))
    agent._request_pressure_anchored = _anchored_pressure is not None
    if _anchored_pressure is not None:
        request_pressure_tokens = _anchored_pressure
    return AssembledRequest(
        "fallthrough", api_messages, tools_for_api, _moa_prepared_request,
        pending_moa_prepared_request, approx_tokens, request_pressure_tokens, approx_tokens * 4,
    )
