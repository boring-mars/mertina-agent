# Ported from hermes-agent agent/turn_api_call.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""The provider call for the conversation turn's retry loop: ``perform_api_call``. Nothing here
imports ``agent.conversation_loop`` at module level (cycle).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class ApiCallVerdict:
    """``action``: ``"fallthrough"`` (``response`` is ready for verification)."""

    action: str
    response: Any


def perform_api_call(
    agent: Any,
    *,
    api_kwargs: Any,
) -> ApiCallVerdict:
    """Issue the request."""
    response = None

    def _verdict(action: str) -> ApiCallVerdict:
        return ApiCallVerdict(
            action=action,
            response=response,
        )

    response = agent._interruptible_api_call(api_kwargs)
    return _verdict("fallthrough")
