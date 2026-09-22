# Ported from hermes-agent agent/system_prompt.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""System-prompt assembly for :class:`AIAgent`.

Built once per session and reused across turns so the upstream prefix cache stays warm. v0.1.0
keeps only the ``context`` tier's caller ``system_message``; the identity, guidance and
timestamp layers come with ``prompt_builder`` in v0.1.1.
"""

from __future__ import annotations

from typing import Any


def _join_tier(parts: list[str | None]) -> str:
    """Join non-empty parts; None/blank entries are dropped."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def build_system_prompt_parts(agent: Any, system_message: str | None = None) -> dict[str, str]:
    """Assemble the system prompt's ``context`` tier (caller ``system_message``)."""
    # ── Context tier (project/worktree-dependent, may change between sessions) ──
    context_parts: list[str] = []
    if system_message is not None:
        context_parts.append(system_message)
    return {
        "context": _join_tier(context_parts),
    }


def build_system_prompt(agent: Any, system_message: str | None = None) -> str:
    """Assemble the full prompt; cached on ``agent._cached_system_prompt``."""
    parts = build_system_prompt_parts(agent, system_message=system_message)
    return "\n\n".join(p for p in (parts["context"],) if p)
