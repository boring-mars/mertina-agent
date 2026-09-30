# Ported from hermes-agent agent/system_prompt.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""System-prompt assembly for :class:`AIAgent`.

Built once per session and reused across turns so the upstream prefix cache stays
warm.  Three tiers are joined with ``\\n\\n``: ``stable`` (identity, tool-use
guidance), ``context`` (caller ``system_message``) and ``volatile`` (timestamp line).
"""

from __future__ import annotations

import re
from typing import Any

from mertina.agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    EXECUTION_GUIDANCE_MODELS,
    GOOGLE_MODEL_OPERATIONAL_GUIDANCE,
    PARALLEL_TOOL_CALL_GUIDANCE,
    TASK_COMPLETION_GUIDANCE,
    TOOL_USE_ENFORCEMENT_GUIDANCE,
    TOOL_USE_ENFORCEMENT_MODELS,
)

_GATE_WORDS = {
    **dict.fromkeys(("true", "always", "yes", "on"), True),
    **dict.fromkeys(("false", "never", "no", "off"), False),
}


def _model_gate(setting: Any, model: str | None, default_models) -> bool:
    """Resolve a config gate: True/"true"-ish -> on, False/"false"-ish -> off,
    list -> case-insensitive model-substring match, anything else ("auto") ->
    match against *default_models*."""
    if setting is True or setting is False:
        return setting
    if isinstance(setting, str) and setting.lower() in _GATE_WORDS:
        return _GATE_WORDS[setting.lower()]
    model_lower = (model or "").lower()
    if isinstance(setting, list):
        return any(p.lower() in model_lower for p in setting if isinstance(p, str))
    return any(p in model_lower for p in default_models)


def _session_start_like(agent: Any, now: Any) -> Any:
    """Best-known conversation start time, or ``now`` as a fallback.
    ``Conversation started:`` must be byte-stable across rebuilds (compression,
    resume, fresh gateway turns), so prefer immutable sources in order: the
    lineage-root session id's embedded stamp (compaction rotates ids, each with
    its own mint time), the current session id's stamp, ``agent.session_start``,
    then ``now``.  Stamps are box-local wall-clock: attach that zone first, then
    convert to ``now``'s zone so the date matches the per-turn clock.

    0. the LINEAGE-ROOT session id's embedded timestamp — compaction can rotate the session id,
    and each rotated id embeds its OWN mint time, so after months of compactions rung 1 alone
    would quietly re-birth the conversation at its latest rotation. Walking to the lineage root
    (same walk as ``_conversation_root_id``) recovers the ORIGINAL birth stamp — a Bot Mode
    forever-chat keeps knowing when it was first born, across every compaction
    (maintainer-directed, #98426); 1. the timestamp embedded in ``session_id``
    (``YYYYMMDD_HHMMSS_...``) — immutable for the life of the session, so the line is byte-stable
    across every rebuild boundary (preserving prefix-cache KV); 2. 3. ``now`` (initial/legacy
    build without either).
    """
    from datetime import datetime

    def _to_display_tz(dt: Any) -> Any:
        if dt.tzinfo is None:
            try:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            except (ValueError, OSError):
                pass
        if getattr(now, "tzinfo", None) is not None and dt.tzinfo is not None:
            try:
                dt = dt.astimezone(now.tzinfo)
            except (ValueError, OSError):
                pass
        return dt

    session_id = getattr(agent, "session_id", None)
    db = getattr(agent, "_session_db", None)
    try:
        root_id = (
            db.get_conversation_root(session_id)
            if db is not None and isinstance(session_id, str) and session_id
            else None
        )
    except Exception:
        root_id = None
    for candidate in (root_id, session_id):
        m = re.match(r"^(\d{8})_(\d{6})", candidate) if isinstance(candidate, str) else None
        if m:
            try:
                return _to_display_tz(
                    datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
                )
            except ValueError:
                pass
    session_start = getattr(agent, "session_start", None)
    return _to_display_tz(session_start) if hasattr(session_start, "astimezone") else now


def _zone_bits(now: Any, tz: Any) -> list[str]:
    """IANA key, abbreviation (if different) and UTC offset — all constant for
    the day, so the byte-stable date line stays cacheable."""
    _iana = getattr(tz, "key", None)
    _abbrev = now.strftime("%Z")
    _offset = now.strftime("%z")  # '-0400' -> 'UTC-04:00'
    bits = [_iana] if _iana else []
    if _abbrev and _abbrev != _iana:
        bits.append(_abbrev)
    if _offset:
        bits.append(f"UTC{_offset[:3]}:{_offset[3:]}")
    return bits


def _timestamp_line(agent: Any) -> str:
    """Date-only so the prompt is byte-stable for the day; zone + offset so
    tools needn't guess EST vs EDT. Long-lived sessions get an "as of" line on
    rebuild days (the cache prefix is already invalidated at that boundary)."""
    from mertina.time import get_timezone as _hermes_tz
    from mertina.time import now as _hermes_now

    now = _hermes_now()
    _bits = _zone_bits(now, _hermes_tz())
    _zone_suffix = f" ({', '.join(_bits)})" if _bits else ""
    _start = _session_start_like(agent, now)
    timestamp_line = f"Conversation started: {_start.strftime('%A, %B %d, %Y')}{_zone_suffix}"
    # Second line (maintainer design, salvaging #96224's anchor): long-lived sessions — Bot Mode
    # forever-chats, messenger channels people never close — span many days and many compactions. A
    # lone birth date leads the model to believe it is still living in that old day. The prompt is
    # rebuilt at every compaction boundary, so stamp the rebuild day too: 'started' stays anchored
    # and byte-stable, 'as of' refreshes exactly when the cache prefix is already being invalidated
    # (compaction), so the added line costs no extra cache churn. Same-day sessions skip the second
    # line entirely — nothing to correct, and the single-line shape stays byte-identical for the day
    # (prefix-cache safe).
    if now.strftime("%Y%m%d") != _start.strftime("%Y%m%d"):
        timestamp_line += (
            f"\nToday's date (as of the last context rebuild): {now.strftime('%A, %B %d, %Y')} "
            "— trust this over the start date for what day it is now; query tools for exact time."
        )
    trailer = (
        ("Session ID", agent.session_id if agent.pass_session_id else None),
        ("Model", agent.model),
        ("Provider", agent.provider),
        ("Platform", agent.platform),
    )
    return timestamp_line + "".join(f"\n{label}: {value}" for label, value in trailer if value)


def _identity_parts(agent: Any) -> tuple[list[str], bool]:
    """The default identity (SOUL.md is not loaded). Returns ``(parts, soul_loaded)``."""
    return ([DEFAULT_AGENT_IDENTITY], False)


def _guidance_parts(agent: Any) -> list[str]:
    """Universal + tool-aware + model-gated guidance blocks, each gated by its config.yaml key."""
    parts: list[str] = []
    if agent.valid_tool_names:
        parts += [
            text
            for flag, text in (
                ("_task_completion_guidance", TASK_COMPLETION_GUIDANCE),
                ("_parallel_tool_call_guidance", PARALLEL_TOOL_CALL_GUIDANCE),
            )
            if getattr(agent, flag, True)
        ]
    if not agent.valid_tool_names:
        return parts
    # agent.tool_use_enforcement / agent.execution_guidance: "auto" (default)
    # matches the hardcoded model lists; true/false force; a list gives custom
    # model-name substrings.  Execution guidance is an independent gate so
    # DeepSeek/Kimi/Qwen-class models get it even with enforcement off.
    if _model_gate(agent._tool_use_enforcement, agent.model, TOOL_USE_ENFORCEMENT_MODELS):
        parts.append(TOOL_USE_ENFORCEMENT_GUIDANCE)
        if any(g in (agent.model or "").lower() for g in ("gemini", "gemma")):
            parts.append(GOOGLE_MODEL_OPERATIONAL_GUIDANCE)
    if _model_gate(
        getattr(agent, "_execution_guidance", "auto"), agent.model, EXECUTION_GUIDANCE_MODELS
    ):
        from mertina.agent.prompt_builder import execution_guidance_text

        parts.append(execution_guidance_text())
    return parts


def _join_tier(parts: list[str | None]) -> str:
    """Join non-empty parts; None/blank entries are dropped."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def build_system_prompt_parts(agent: Any, system_message: str | None = None) -> dict[str, str]:
    """Assemble the system prompt as three ordered cache tiers: ``stable`` (identity and
    guidance), ``context`` (caller ``system_message``) and ``volatile`` (timestamp line).
    Never re-rendered mid-session."""
    # ── Stable tier ────────────────────────────────────────────────
    stable_parts, _soul_loaded = _identity_parts(agent)
    stable_parts.extend(_guidance_parts(agent))
    # ── Context tier (project/worktree-dependent, may change between sessions) ──
    context_parts: list[str] = []
    if system_message is not None:
        context_parts.append(system_message)
    # ── Volatile tier (kept last so the stable prefix stays reusable) ──
    volatile_parts: list[str] = []
    volatile_parts.append(_timestamp_line(agent))
    return {
        "stable": _join_tier(stable_parts),
        "context": _join_tier(context_parts),
        "volatile": _join_tier(volatile_parts),
    }


def build_system_prompt(agent: Any, system_message: str | None = None) -> str:
    """Assemble the full prompt; cached on ``agent._cached_system_prompt``.  Tiers are
    ordered stable -> context -> volatile so implicit longest-prefix caches keep the
    unchanged scaffold."""
    parts = build_system_prompt_parts(agent, system_message=system_message)
    return "\n\n".join(p for p in (parts["stable"], parts["context"], parts["volatile"]) if p)


__all__ = [
    "build_system_prompt",
    "build_system_prompt_parts",
]
