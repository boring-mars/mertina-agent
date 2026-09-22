# Ported from hermes-agent agent/turn_truncation.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Response normalization for the conversation turn loop (upstream's home for truncation
recovery, ``finish_reason == "length"``, which v0.1 does not port)."""

from __future__ import annotations

from typing import Any


def normalize_response_for_agent(agent: Any, response: Any) -> Any:
    """One OpenAI-style message from the transport."""
    return agent._get_transport().normalize_response(response)
