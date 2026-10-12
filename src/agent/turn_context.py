"""Per-turn setup for ``run_conversation`` (the turn prologue).

``build_turn_context`` runs the once-per-turn setup (stdio guard, sanitization, prompt
restore-or-build, session row, idle/preflight compaction via ``turn_context_compaction``,
pre_llm_call hook, prefetch, persistence), mutating ``agent`` as the loop expects, and
returns a ``TurnContext`` with only the locals the loop reads back.
``build_api_messages`` builds the wire copy for one API call."""

from __future__ import annotations

import logging
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agent.iteration_budget import IterationBudget
from agent.message_metadata import append_message, stamp_message_timestamp
from agent.turn_author import parse_turn_author

logger = logging.getLogger(__name__)


def _str_attr(agent: Any, name: str) -> str:
    """``getattr(agent, name, "") or ""`` — route facts read off partial agents/doubles."""
    return getattr(agent, name, "") or ""




def _agent_stale_thinking_on_wire(agent: Any) -> bool:
    """Whether the active route replays stale thinking text; ``True`` (conservative full
    charge) when route facts are unavailable."""
    try:
        from agent.message_sanitization import stale_thinking_reaches_wire

        return stale_thinking_reaches_wire(
            *(_str_attr(agent, k) for k in ("api_mode", "provider", "model", "base_url"))
        )
    except Exception:
        return True


def compose_user_api_content(
    content: Any, ext_prefetch_cache: str, plugin_user_context: str
) -> Optional[str]:
    """Compose the API-bound content of the current turn's user message.

    Single source for the ``api_content`` sidecar and the wire bytes so they never drift
    (what turn N sends is what turn N+1 replays). ``None`` when nothing is injected."""
    if not isinstance(content, str):
        return None
    injections = [part for part in (plugin_user_context,) if part]
    if not injections:
        return None
    return content + "\n\n" + "\n\n".join(injections)


def substitute_api_content(api_msg: Dict[str, Any]) -> Optional[str]:
    """Pop the ``api_content`` sidecar and substitute it into ``content`` (keeps the
    prompt-cache prefix byte-stable). Returns the popped sidecar, or ``None``."""
    sidecar = api_msg.pop("api_content", None)
    if isinstance(sidecar, str) and sidecar and api_msg.get("role") in ("user", "assistant"):
        api_msg["content"] = sidecar
    return sidecar


def drop_stale_api_content(msg: Dict[str, Any]) -> None:
    """Drop the ``api_content`` sidecar from a message whose content was rewritten
    (replaying it would resend what the rewrite removed; cost is one cache miss)."""
    msg.pop("api_content", None)












# Surfaces whose sessions must not be auto-titled: cron names its own session and
# its opener is a delivery hint; subagent sessions are hidden from every picker.
_UNTITLED_PLATFORMS = frozenset({"cron", "subagent"})












class PreflightCompressionTimedOut(RuntimeError):
    """Raised when an oversized turn cannot safely finish preflight."""














@dataclass
class TurnContext:
    """Values produced by the turn prologue and consumed by the turn loop."""

    user_message: str  # sanitized inbound message (surrogates stripped)
    original_user_message: Any  # clean text for transcripts / memory queries (no nudges)
    messages: List[Dict[str, Any]]  # working list for this turn (loop appends to it)
    conversation_history: Optional[List[Dict[str, Any]]]  # None after rotation
    active_system_prompt: Optional[str]  # may be rebuilt by compression
    effective_task_id: str
    turn_id: str
    current_turn_user_idx: int  # index of the current user turn within ``messages``
    should_review_memory: bool = False  # post-turn memory review should fire
    plugin_user_context: str = ""  # ``pre_llm_call`` context (appended to user message)
    ext_prefetch_cache: str = ""  # external-memory prefetch, reused across iterations
    preflight_compression_blocked: bool = False  # immediate retry proved ineffective








def _bind_turn_identity(
    agent: Any, task_id: Optional[str], stream_callback, persist_user_message: Any,
    persist_user_timestamp: Optional[float], persist_user_platform_id: Optional[str],
) -> Tuple[str, str]:
    """Stage callback/persist overrides on the agent and bind this turn's task and turn
    ids. Returns ``(effective_task_id, turn_id)``."""
    agent._stream_callback = stream_callback  # picked up by _interruptible_api_call
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = persist_user_message
    agent._persist_user_message_timestamp = persist_user_timestamp
    agent._persist_user_message_platform_id = persist_user_platform_id
    # Unique task_id when not provided isolates VMs between tasks.
    effective_task_id = task_id or str(uuid.uuid4())
    agent._current_task_id = effective_task_id
    agent._process_owner_task_ids = {*getattr(agent, "_process_owner_task_ids", ()), effective_task_id}
    turn_id = str(getattr(agent, "_relay_pending_turn_id", "") or "") or (
        f"{agent.session_id or 'session'}:{effective_task_id}:{uuid.uuid4().hex[:8]}"
    )
    agent._relay_pending_turn_id = None
    agent._current_turn_id = turn_id
    agent._current_api_request_id = ""
    return effective_task_id, turn_id


# Per-turn agent state reset at turn start (retry counters, guardrail halt, file-mutation
# verifier). ``_turns_since_memory`` / ``_iters_since_skill`` are deliberately NOT reset.
_PER_TURN_RESET_STATE: Tuple[Tuple[str, Any], ...] = (
    ("_invalid_tool_retries", 0), ("_invalid_json_retries", 0), ("_empty_content_retries", 0),
    ("_incomplete_scratchpad_retries", 0), ("_codex_incomplete_retries", 0),
    # Consecutive Codex reasoning-only (no answer, no tool call) responses, kept apart from
    # the aggregate incomplete count so a visible partial resets it (#67321).
    ("_codex_reasoning_only_streak", 0),
    ("_thinking_prefill_retries", 0), ("_post_tool_empty_retried", False),
    ("_last_content_with_tools", None), ("_last_content_tools_all_housekeeping", False),
    ("_mute_post_response", False), ("_unicode_sanitization_passes", 0),
    ("_iteration_budget_warning_injected", False),
    ("_run_budget_wrapup_injected", False), ("_verification_stop_nudges", 0),
    ("_pre_verify_nudges", 0),
)


def _reset_per_turn_agent_state(agent: Any) -> None:
    """Reset retry counters, guardrails, iteration and run budgets at turn start."""
    for name, value in _PER_TURN_RESET_STATE:
        setattr(agent, name, value)
    agent._turn_failed_file_mutations = {}
    agent._turn_file_mutation_paths = set()
    # Expiry clock for build_api_messages: admission time (not the input's platform-event
    # stamp, which can predate admission by minutes), frozen so every request this turn
    # sends identical bytes. Distinct from note_turn_start's _inflight_turn_started, a
    # tripwire slot cleared at persist.
    agent._current_turn_timestamp = time.time()

    # Pre-turn connection health check: clean up dead TCP connections.
    if agent.api_mode != "anthropic_messages":
        with suppress(Exception):
            if agent._cleanup_dead_connections():
                agent._emit_diagnostic_status(
                    "🔌 Detected stale connections from a previous provider "
                    "issue — cleaned up automatically. Proceeding with fresh "
                    "connection."
                )
    agent.iteration_budget = IterationBudget(agent.max_iterations)
    # Wall-clock run budget: stamped only when configured (one wrap-up notice per run).
    agent._run_budget_started_at = (
        time.time() if getattr(agent, "run_budget_seconds", None) else None
    )
    # Reset the streaming context / think scrubbers at the top of each turn.
    for name in ("_stream_context_scrubber", "_stream_think_scrubber"):
        scrubber = getattr(agent, name, None)
        if scrubber is not None:
            scrubber.reset()


def _stage_turn_user_message(
    agent: Any, user_message: Any, persist_user_message: Any,
    persist_user_timestamp: Optional[float], persist_user_platform_id: Optional[str],
    persist_user_display_kind: Optional[str],
    persist_user_display_metadata: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Any]:
    """Build this turn's user dict, reusing CLI-staged input only when its clean text
    matches this turn (a stale handoff must not replace later input; voice turns
    compare the clean override). Returns ``(user_msg, pending_cli_message)``."""
    pending_cli_message = getattr(agent, "_pending_cli_user_message", None)
    expected_persist_content = (
        persist_user_message if persist_user_message is not None else user_message
    )
    if (
        isinstance(pending_cli_message, dict)
        and pending_cli_message.get("content") == expected_persist_content
    ):
        user_msg = pending_cli_message
        # CLI-staged value is the clean text; restore the API-facing variant (e.g. voice
        # prefix) on the same dict, keeping any close-path durable marker.
        user_msg["content"] = user_message
    else:
        user_msg = {"role": "user", "content": user_message}
        if isinstance(pending_cli_message, dict):
            agent._pending_cli_user_message = None
    # CLI input is stamped when staged; gateway input may carry the platform event
    # time. Preserve either value and cover any legacy unstamped handoff.
    stamp_message_timestamp(user_msg, timestamp=persist_user_timestamp)

    # Synthesized turns stamp their transcript type so the crash persist writes a typed
    # row; the model still receives role/content unchanged (api_messages strips both).
    if persist_user_display_kind:
        user_msg["display_kind"] = persist_user_display_kind
    if persist_user_display_metadata:
        user_msg["display_metadata"] = persist_user_display_metadata
    # The platform message id survives the turn-start flush; restart drain-window
    # recovery dedups via ``has_platform_message_id`` against this row.
    if persist_user_platform_id is not None:
        user_msg["platform_message_id"] = persist_user_platform_id
    return user_msg, pending_cli_message














def _bind_interrupt_scope(agent: Any, ra) -> None:
    """Record the execution thread so interrupt()/clear_interrupt() scope the tool-level
    signal to THIS agent's thread; clear stale state, preserving a pending interrupt."""
    agent._execution_thread_id = threading.current_thread().ident
    ra()._set_interrupt(False, agent._execution_thread_id)
    if agent._interrupt_requested:
        ra()._set_interrupt(
            True, agent._execution_thread_id, reason=getattr(agent, "_tool_interrupt_reason", None)
        )
    else:
        agent._interrupt_message = None
        agent._tool_interrupt_reason = None
    agent._interrupt_thread_signal_pending = False








def build_turn_context(
    agent, user_message: Any, system_message: Optional[str],
    conversation_history: Optional[List[Dict[str, Any]]], task_id: Optional[str], stream_callback,
    persist_user_message: Optional[Any], persist_user_timestamp: Optional[float]=None,
    persist_user_platform_id: Optional[str]=None, *, persist_user_display_kind: Optional[str]=None,
    persist_user_display_metadata: Optional[Dict[str, Any]]=None, turn_author: Optional[Dict[str, Any]]=None,
    restore_or_build_system_prompt,
    install_safe_stdio, sanitize_surrogates, summarize_user_message_for_log, set_session_context,
    set_current_write_origin, ra, moa_active: bool=False,
) -> TurnContext:
    """Run the once-per-turn setup and return the loop's input context.

    Helpers are passed in to avoid an import cycle with ``agent.conversation_loop``.
    Order matters: the DB session row is created only AFTER the system prompt is built
    (else it persists system_prompt=NULL and costs a cache miss) and BEFORE preflight
    compression."""
    # Guard stdio against OSError from broken pipes (systemd/headless/daemon).
    install_safe_stdio()

    # Reset first: a cached gateway agent must never carry the previous turn's bot author into a human turn.
    turn_author = parse_turn_author(turn_author)
    agent._turn_author = turn_author

    # Tag log records on this thread with the session ID for ``hermes logs``; bind the
    # skill write-origin ContextVar; restore the primary runtime after a fallback turn.
    # NOTE: the DB session row is created later, AFTER the system prompt is restored/built (see
    # _ensure_db_session() below the system-prompt block). Creating it here — before _cached_system_prompt
    # is populated — inserts a row with system_prompt=NULL on a fresh API/gateway agent that carries
    # client-managed history, which then trips the "stored system prompt is null; rebuilding from scratch"
    # warning and a needless first-turn prefix cache miss. (Issue #45499.)
    set_session_context(agent.session_id)
    set_current_write_origin(getattr(agent, "_memory_write_origin", "assistant_tool"))
    if isinstance(user_message, str):
        user_message = sanitize_surrogates(user_message)
    if isinstance(persist_user_message, str):
        persist_user_message = sanitize_surrogates(persist_user_message)

    effective_task_id, turn_id = _bind_turn_identity(
        agent, task_id, stream_callback, persist_user_message,
        persist_user_timestamp, persist_user_platform_id,
    )
    _reset_per_turn_agent_state(agent)

    _preview_text = summarize_user_message_for_log(user_message)
    _msg_preview = _preview_text[:80] + ("..." if len(_preview_text) > 80 else "")
    logger.info(
        "conversation turn: session=%s model=%s provider=%s platform=%s history=%d msg=%r",
        agent.session_id or "none", agent.model, agent.provider or "unknown",
        agent.platform or "unknown", len(conversation_history or []),
        _msg_preview.replace("\n", " "),
    )

    # Copy so the caller's list is never mutated.
    messages = list(conversation_history) if conversation_history else []
    user_msg, pending_cli_message = _stage_turn_user_message(
        agent, user_message, persist_user_message, persist_user_timestamp,
        persist_user_platform_id, persist_user_display_kind, persist_user_display_metadata,
    )
    # Append the user message now that close persistence is safe.
    append_message(messages, user_msg)
    current_turn_user_idx = len(messages) - 1
    agent._persist_user_message_idx = current_turn_user_idx

    agent._user_turn_count += 1
    # Copilot x-initiator: the first API call of this user turn is user-initiated;
    # tool-loop follow-ups revert to "agent".
    agent._is_user_initiated_turn = True

    # Preserve the original user message (no nudge injection).
    original_user_message = persist_user_message if persist_user_message is not None else user_message
    if not agent.quiet_mode:
        agent._safe_print(
            f"💬 Starting conversation: '{_preview_text[:60]}"
            f"{'...' if len(_preview_text) > 60 else ''}'"
        )

    # System prompt is cached per session for prefix caching.
    if agent._cached_system_prompt is None:
        restore_or_build_system_prompt(agent, system_message, conversation_history)
    active_system_prompt = agent._cached_system_prompt

    _bind_interrupt_scope(agent, ra)

    return TurnContext(
        user_message=user_message, original_user_message=original_user_message, messages=messages,
        conversation_history=conversation_history, active_system_prompt=active_system_prompt,
        effective_task_id=effective_task_id, turn_id=turn_id,
        current_turn_user_idx=current_turn_user_idx,
    )




def build_api_messages(
    agent: Any, messages: List[Dict[str, Any]], *, current_turn_user_idx: Any,
    ext_prefetch_cache: Any, plugin_user_context: Any, moa_config: Any, active_system_prompt: Any,
) -> Tuple[List[Dict[str, Any]], str]:
    """Build the wire copy of ``messages`` for one API call plus the effective system
    message. Returns ``(api_messages, effective_system)``.

    Prompt-cache invariant: historical user/assistant rows replay their ``api_content``
    sidecar (the exact bytes sent live) so the prefix stays byte-stable; the current
    user turn reuses the prologue's stamp (or composes live when a caller bypassed the
    prologue). Ephemeral context (prefetch, ``pre_llm_call`` hooks,
    ``ephemeral_system_prompt``) is added at API time only — ``messages`` stays untouched
    beyond the sidecar stamp, and the system prompt is built ONCE per session and
    replayed verbatim."""
    from agent.agent_runtime_helpers import fill_empty_non_final_wire_payload
    from agent.conversation_loop import _clone_message_for_send
    from agent.replay_cleanup import canonicalize_replay_history

    has_current = isinstance(current_turn_user_idx, int) and 0 <= current_turn_user_idx < len(messages)
    current_turn_message = messages[current_turn_user_idx] if has_current else None

    # Replay consumers canonicalize the persisted prefix on read; the request copy must
    # carry the same bytes or a resume diverges mid-prefix. Only the rows BEFORE this
    # turn's user message are the replayed prefix — rows this turn appended (its tool
    # calls/results) are live and must never be rewritten between iterations. The
    # expiry clock is the turn's admission time, frozen in _reset_per_turn_agent_state.
    # Without an anchor (compaction found no surviving user row) there is no provable
    # persisted prefix, so nothing is canonicalized. The clock is stamped once per turn in
    # _reset_per_turn_agent_state; a caller that skipped the prologue fails loudly here
    # rather than silently un-freezing it.
    turn_now = agent._current_turn_timestamp
    split = current_turn_user_idx if has_current else 0
    canonical_messages = canonicalize_replay_history(messages[:split], now=turn_now) + messages[split:]

    api_messages = []
    for idx, msg in enumerate(canonical_messages):
        # Structural clone, NOT msg.copy(): in-place transforms below must not reach
        # persisted history via nested containers; see _clone_message_for_send.
        api_msg = _clone_message_for_send(msg)
        # api_content is bookkeeping (exact bytes sent), never a provider field — pop
        # it from EVERY outgoing copy. display_* is display-only timeline metadata
        # (strict OpenAI backends reject unknown keys); _row_id is the durable row id
        # from _rows_to_conversation and only chat-completions strips underscore keys.
        _api_content = api_msg.pop("api_content", None)
        for key in ("display_kind", "display_metadata", "_row_id"):
            api_msg.pop(key, None)

        # Inject ephemeral context (memory prefetch + pre_llm_call user hooks)
        # at API time only; `messages` is untouched beyond the api_content stamp.
        if msg is current_turn_message and msg.get("role") == "user":
            if isinstance(_api_content, str) and _api_content:
                # Reuse the prologue's stamp so sidecar and wire cannot drift
                # and every pass this turn sends identical bytes.
                api_msg["content"] = _api_content
            else:
                # Callers that bypass the prologue stamping: compose live.
                _composed = compose_user_api_content(
                    api_msg.get("content", ""), ext_prefetch_cache, plugin_user_context
                )
                if _composed is not None:
                    api_msg["content"] = _composed
        elif (
            isinstance(_api_content, str) and _api_content
            and msg.get("role") in ("user", "assistant")
        ):
            # Historical row: replay the exact bytes sent live so the prompt-cache
            # prefix stays byte-stable. User rows carry the injection sidecar; user
            # and assistant rows may carry a sanitize-divergence sidecar.
            api_msg["content"] = _api_content

        # 'reasoning' is trajectory-only (copied to 'reasoning_content' above);
        # finish_reason is rejected by strict APIs (e.g. Mistral).
        api_msg.pop("reasoning", None)
        api_msg.pop("finish_reason", None)
        # Fill empty non-final user/assistant wire copies so the pre-call sanitizer
        # stops re-healing and flooding errors.log; durable history is untouched.
        # After the reasoning copy so thinking-only turns keep payload.
        fill_empty_non_final_wire_payload(api_msg, is_final=(idx == len(canonical_messages) - 1))
        # _thinking_prefill survives intentionally: the drop pass below needs it.
        # Strip length-continuation marks; some transports keep underscore keys.
        api_msg.pop("_length_continuation_fragment", None)
        api_msg.pop("_length_continuation_nudge", None)
        # 'reasoning_details' is kept here; the chat-completions transport drops it on the
        # wire for every route that does not replay it (OpenRouter/Nous do).
        api_messages.append(api_msg)

    # Final system message = cached prompt + ephemeral additions (API-time only).
    # Plugin/recall context goes into the user message, never the system prompt: the
    # prompt is built ONCE per session and replayed verbatim (stable cache prefix).
    effective_system = active_system_prompt or ""
    if agent.ephemeral_system_prompt:
        effective_system = (effective_system + "\n\n" + agent.ephemeral_system_prompt).strip()
    if effective_system:
        api_messages = [{"role": "system", "content": effective_system}] + api_messages
    return api_messages, effective_system


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.


_PLUGIN_COMPAT_LAZY = {
    'IDLE_COMPACTION_STATUS_TEMPLATE': ('agent.conversation_compression', 'IDLE_COMPACTION_STATUS_TEMPLATE'),
    'PREFLIGHT_COMPRESSION_STATUS_TEMPLATE': ('agent.conversation_compression', 'PREFLIGHT_COMPRESSION_STATUS_TEMPLATE'),
    'automatic_compaction_status_message': ('agent.context_engine', 'automatic_compaction_status_message'),
    'compression_skipped_due_to_lock': ('agent.conversation_compression', 'compression_skipped_due_to_lock'),
    'conversation_history_after_compression': ('agent.conversation_compression', 'conversation_history_after_compression'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
