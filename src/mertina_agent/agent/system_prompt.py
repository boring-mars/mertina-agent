"""Assemble the system prompt once per agent from ordered, cache-friendly tiers.

Copied from Hermes agent/system_prompt.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md for the pinned source and reductions.

The tier order (stable, context, volatile) is kept so a longest-prefix cache can
reuse the unchanged scaffold. v0.1 fills the stable tier with identity and tool
guidance, the context tier with the caller's system message and the volatile
tier with the timestamp line; skills, memory, context files, plugin sections and
environment hints are not part of this milestone. Hermes reads everything from
the agent object; here each input is an explicit argument and the clock is
injected so the prompt is deterministic under test.
"""

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import TypedDict

from mertina_agent.agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    GOOGLE_MODEL_OPERATIONAL_GUIDANCE,
    PARALLEL_TOOL_CALL_GUIDANCE,
    TASK_COMPLETION_GUIDANCE,
    TOOL_USE_ENFORCEMENT_GUIDANCE,
    TOOL_USE_ENFORCEMENT_MODELS,
)

type ModelGate = bool | str | Sequence[str]
"""A config gate: on/off, an on/off word, model-name substrings, or ``"auto"``."""

_GATE_WORDS = {
    **dict.fromkeys(("true", "always", "yes", "on"), True),
    **dict.fromkeys(("false", "never", "no", "off"), False),
}


class SystemPromptParts(TypedDict):
    """The system prompt split into its cache tiers, in send order."""

    stable: str
    context: str
    volatile: str


def _model_gate(setting: ModelGate, model: str, default_models: Sequence[str]) -> bool:
    """Resolve a gate: booleans and on/off words force it, a list matches model substrings.

    Anything else (``"auto"``) matches ``model`` against ``default_models``.
    """
    if setting is True or setting is False:
        return setting
    if isinstance(setting, str):
        if setting.lower() in _GATE_WORDS:
            return _GATE_WORDS[setting.lower()]
        return any(pattern in model.lower() for pattern in default_models)
    return any(pattern.lower() in model.lower() for pattern in setting)


def _join_tier(parts: Sequence[str | None]) -> str:
    """Join non-empty parts; ``None`` and blank entries are dropped."""
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _zone_bits(now: datetime) -> list[str]:
    """IANA key, abbreviation (if different) and UTC offset of ``now``.

    All are constant for the day, so the date-only timestamp line stays
    byte-stable and cacheable.
    """
    iana = getattr(now.tzinfo, "key", None)
    abbreviation = now.strftime("%Z")
    offset = now.strftime("%z")  # '-0400' -> 'UTC-04:00'
    bits = [iana] if isinstance(iana, str) and iana else []
    if abbreviation and abbreviation != iana:
        bits.append(abbreviation)
    if offset:
        bits.append(f"UTC{offset[:3]}:{offset[3:]}")
    return bits


def _timestamp_line(now: datetime, model: str) -> str:
    """Date-only, so the prompt is byte-stable for the day, with the zone and UTC offset."""
    bits = _zone_bits(now)
    zone_suffix = f" ({', '.join(bits)})" if bits else ""
    return f"Conversation started: {now.strftime('%A, %B %d, %Y')}{zone_suffix}\nModel: {model}"


def _guidance_parts(
    valid_tool_names: Collection[str], model: str, tool_use_enforcement: ModelGate
) -> list[str]:
    """Universal and model-gated tool guidance; nothing at all when no tools are offered."""
    if not valid_tool_names:
        return []
    parts = [TASK_COMPLETION_GUIDANCE, PARALLEL_TOOL_CALL_GUIDANCE]
    if _model_gate(tool_use_enforcement, model, TOOL_USE_ENFORCEMENT_MODELS):
        parts.append(TOOL_USE_ENFORCEMENT_GUIDANCE)
        if any(family in model.lower() for family in ("gemini", "gemma")):
            parts.append(GOOGLE_MODEL_OPERATIONAL_GUIDANCE)
    return parts


def build_system_prompt_parts(
    *,
    model: str,
    valid_tool_names: Collection[str],
    now: datetime,
    system_message: str | None = None,
    tool_use_enforcement: ModelGate = "auto",
) -> SystemPromptParts:
    """Assemble the prompt as three ordered cache tiers.

    Args:
        model: Model name; selects model-gated guidance and is stated in the prompt.
        valid_tool_names: Tools offered this conversation; guidance only appears with tools.
        now: Timezone-aware conversation start time.
        system_message: Caller instructions placed in the context tier.
        tool_use_enforcement: Gate for the tool-use enforcement block.
    """
    stable_parts = [
        DEFAULT_AGENT_IDENTITY,
        *_guidance_parts(valid_tool_names, model, tool_use_enforcement),
    ]
    context_parts = [system_message]
    volatile_parts = [_timestamp_line(now, model)]
    return {
        "stable": _join_tier(stable_parts),
        "context": _join_tier(context_parts),
        "volatile": _join_tier(volatile_parts),
    }


def build_system_prompt(
    *,
    model: str,
    valid_tool_names: Collection[str],
    now: datetime,
    system_message: str | None = None,
    tool_use_enforcement: ModelGate = "auto",
) -> str:
    """Assemble the full prompt, tiers ordered stable, context, volatile.

    The agent builds it once per conversation and replays it verbatim, so the
    request prefix stays stable across turns.
    """
    parts = build_system_prompt_parts(
        model=model,
        valid_tool_names=valid_tool_names,
        now=now,
        system_message=system_message,
        tool_use_enforcement=tool_use_enforcement,
    )
    return "\n\n".join(
        tier for tier in (parts["stable"], parts["context"], parts["volatile"]) if tier
    )
