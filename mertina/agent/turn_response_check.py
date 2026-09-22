# Ported from hermes-agent agent/turn_response_check.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Post-call response verification for the conversation turn's retry loop: report the call's
latency and fold usage into the session counters. Nothing here imports
``agent.conversation_loop`` at module level (cycle).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from mertina.agent.turn_usage import record_response_usage

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class ResponseCheckVerdict:
    """``action``: ``"break"`` (leave the retry loop). The other fields are the retry-loop
    locals rebound."""

    action: str
    api_duration: Any


def check_api_response(
    agent: Any,
    *,
    response: Any,
    api_start_time: Any,
) -> ResponseCheckVerdict:
    """Verify ``response``."""

    def _verdict(action: str) -> ResponseCheckVerdict:
        return ResponseCheckVerdict(
            action=action,
            api_duration=api_duration,
        )

    api_duration = time.time() - api_start_time

    if not agent.quiet_mode:
        agent._vprint(f"{agent.log_prefix}⏱️  API call completed in {api_duration:.2f}s")

    if agent.verbose_logging:
        resp_model = getattr(response, "model", "N/A") if response else "N/A"
        logging.debug(
            f"API Response received - Model: {resp_model}, Usage: {response.usage if hasattr(response, 'usage') else 'N/A'}"  # noqa: E501  # upstream's message
        )

    # Fold provider usage into session counters (agent/turn_usage.py).
    record_response_usage(
        agent,
        response,
        api_duration=api_duration,
    )

    return _verdict("break")
