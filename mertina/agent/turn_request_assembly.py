# Ported from hermes-agent agent/turn_request_assembly.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Per-iteration API request assembly for the conversation turn loop: build ``api_messages``
from the transcript. Nothing here imports ``agent.conversation_loop`` at module level (cycle).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mertina.agent.turn_context import build_api_messages

logger = logging.getLogger("mertina.agent.conversation_loop")


@dataclass
class AssembledRequest:
    """Always ``action == "fallthrough"``; the fields are the iteration locals the assembly
    produces."""

    action: str
    api_messages: Any
    tools_for_api: Any


def assemble_api_request(
    agent: Any,
    *,
    messages: Any,
    active_system_prompt: Any,
) -> AssembledRequest:
    """Assemble the request."""
    api_messages, effective_system = build_api_messages(  # noqa: RUF059  # upstream unpacks both
        agent,
        messages,
        active_system_prompt=active_system_prompt,
    )

    tools_for_api = agent.tools

    return AssembledRequest(
        "fallthrough",
        api_messages,
        tools_for_api,
    )
