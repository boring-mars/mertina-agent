"""API error classification for smart failover and recovery.

A priority-ordered pipeline maps an API exception to a ``ClassifiedError``
whose recovery hints (retry, rotate credential, fallback, compress, abort) the
retry loop in run_agent.py consults instead of re-matching strings itself.
"""

from __future__ import annotations

import enum
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional, Sequence

logger = logging.getLogger(__name__)

# Synthetic code for the OpenAI SDK rejecting a provider's SSE ``data:`` field
# before any completion chunk arrives; distinct from generic JSON parse errors.
PROVIDER_STREAM_NON_JSON_ERROR_CODE = "provider_stream_non_json_data"

# Same rejection with an EMPTY payload: the frame carried no ``data`` at all (``data:`` /
# ``event: ping`` / ``id:`` with no content). Per the SSE spec those are legal keepalives /
# no-ops, not malformed payloads — a degraded gateway answers every streaming request with
# them, so the session switches to non-streaming instead of re-streaming into the same
# window. See ``chat_completion_helpers._maybe_disable_streaming``.
PROVIDER_STREAM_EMPTY_FRAME_ERROR_CODE = "provider_stream_empty_frame"


# ── Error taxonomy ──────────────────────────────────────────────────────

class FailoverReason(enum.Enum):
    """Why an API call failed — determines recovery strategy."""
    auth = "auth"                        # Transient auth (401/403) — refresh/rotate
    auth_permanent = "auth_permanent"    # Auth failed after refresh — abort
    billing = "billing"                  # 402 or confirmed credit exhaustion — rotate immediately
    rate_limit = "rate_limit"            # 429 or quota-based throttling — backoff then rotate
    upstream_rate_limit = "upstream_rate_limit"  # Aggregator's upstream model 429 — fallback model, key is healthy
    upstream_blocked = "upstream_blocked"  # 403 from a WAF/CDN/proxy in front of the provider — key is healthy, fallback
    overloaded = "overloaded"            # 503/529 — provider overloaded, backoff
    server_error = "server_error"        # 500/502 — internal server error, retry
    timeout = "timeout"                  # Connection/read timeout — rebuild client + retry
    ssl_cert_verification = "ssl_cert_verification"  # Deterministic TLS chain failure — fail fast with guidance
    context_overflow = "context_overflow"  # Context too large — compress, not failover
    payload_too_large = "payload_too_large"  # 413 — compress payload
    image_too_large = "image_too_large"   # Native image part exceeds provider's per-image limit — shrink and retry
    image_corrupt = "image_corrupt"       # Provider can't decode image bytes — strip and retry (shrinking won't help)
    model_not_found = "model_not_found"  # 404 or invalid model — fallback to different model
    provider_policy_blocked = "provider_policy_blocked"  # Aggregator account data/privacy policy excluded the only endpoint
    content_policy_blocked = "content_policy_blocked"  # Provider safety filter rejected this prompt — deterministic per-request, don't retry unchanged
    model_entitlement = "model_entitlement"  # This account cannot use the requested model — rotate credential (model-scoped), else fall back
    incomplete_response = "incomplete_response"  # Codex/Responses turn stuck emitting reasoning only (no answer, no tool call) after replay + nudge — hand to a different provider
    format_error = "format_error"        # 400 bad request — abort or strip + retry
    role_alternation = "role_alternation"  # Strict chat template rejected adjacent same-role messages — merge them for this destination and retry
    invalid_encrypted_content = "invalid_encrypted_content"  # Responses replay blob rejected — strip replay state and retry
    multimodal_tool_content_unsupported = "multimodal_tool_content_unsupported"  # Provider rejected list-type content in tool messages (e.g. Xiaomi MiMo) — downgrade to text and retry
    reasoning_mandatory = "reasoning_mandatory"  # Route rejects reasoning: {enabled: false} — send the disable no more this session and retry

    # Provider-specific
    thinking_signature = "thinking_signature"  # Anthropic thinking block sig invalid
    long_context_tier = "long_context_tier"    # Anthropic "extra usage" tier gate
    oauth_long_context_beta_forbidden = "oauth_long_context_beta_forbidden"  # Anthropic OAuth rejects 1M beta — disable beta and retry
    llama_cpp_grammar_pattern = "llama_cpp_grammar_pattern"  # llama.cpp grammar rejects regex `pattern`/`format` — strip from tools and retry
    unknown = "unknown"                  # Unclassifiable — retry with backoff
