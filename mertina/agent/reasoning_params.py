# Ported from hermes-agent agent/reasoning_params.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Assistant-message building and ``reasoning_content`` replay for ``AIAgent`` (upstream's home
for the provider reasoning-parameter policy, which v0.1 does not port).
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""

from mertina.agent.lazy_forward import forward as _forward


class ReasoningParamsMixin:
    """Assistant-message building and reasoning replay (see module docstring)."""

    _build_assistant_message = _forward(
        "mertina.agent.chat_completion_helpers", "build_assistant_message"
    )

    _copy_reasoning_content_for_api = _forward(
        "mertina.agent.agent_runtime_helpers", "copy_reasoning_content_for_api"
    )
