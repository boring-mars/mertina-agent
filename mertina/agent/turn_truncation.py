# Ported from hermes-agent agent/turn_truncation.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Truncation recovery (``finish_reason == "length"``) for the conversation turn loop.

Handles thinking-budget exhaustion, repetition-dominated truncation, content-filter stream
stalls escalated to the fallback chain, text continuation nudges (up to 4, with the ceiling
exit that drops the fragment trail), truncated tool-call retries with max_tokens boosts, and
the final roll-back. Nothing here imports ``agent.conversation_loop`` at module level
(cycle); loop-internal helpers are imported lazily so tests patching them keep working.
"""

from __future__ import annotations

from typing import Any


def normalize_response_for_agent(agent: Any, response: Any) -> Any:
    """One OpenAI-style message from any transport; Anthropic strips the OAuth tool prefix."""
    if agent.api_mode == "anthropic_messages":
        return agent._get_transport().normalize_response(
            response, strip_tool_prefix=agent._is_anthropic_oauth
        )
    return agent._get_transport().normalize_response(response)
