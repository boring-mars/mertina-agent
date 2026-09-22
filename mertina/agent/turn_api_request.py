# Ported from hermes-agent agent/turn_api_request.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Per-attempt request assembly for the conversation turn's retry loop: build ``api_kwargs``.
Nothing here imports ``agent.conversation_loop`` at module level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class ApiRequestBuild:
    """Always ``action == "fallthrough"``; the fields are the request-local values the caller
    rebinds for the attempt."""

    action: str
    api_kwargs: Any


def build_api_request(
    agent: Any,
    *,
    api_messages: Any,
    tools_for_api: Any,
) -> ApiRequestBuild:
    """Assemble the attempt's request."""
    if tools_for_api == agent.tools:
        api_kwargs = agent._build_api_kwargs(api_messages)
    else:
        api_kwargs = agent._build_api_kwargs(api_messages, tools_for_api=tools_for_api)
    return ApiRequestBuild(
        "fallthrough",
        api_kwargs,
    )
