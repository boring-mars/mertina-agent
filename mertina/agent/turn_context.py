# Ported from hermes-agent agent/turn_context.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Per-turn setup for ``run_conversation`` (the turn prologue).

``build_turn_context`` runs the once-per-turn setup (sanitization, prompt build), mutating
``agent`` as the loop expects, and returns a ``TurnContext`` with only the locals the loop
reads back. ``build_api_messages`` builds the wire copy for one API call."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mertina.agent.iteration_budget import IterationBudget
from mertina.agent.message_metadata import append_message

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    """Values produced by the turn prologue and consumed by the turn loop."""

    messages: list[dict[str, Any]]  # working list for this turn (loop appends to it)
    active_system_prompt: str | None
    effective_task_id: str


def _bind_turn_identity(
    agent: Any,
    task_id: str | None,
) -> str:
    """Bind this turn's task id. Returns ``effective_task_id``."""
    # Unique task_id when not provided isolates VMs between tasks.
    effective_task_id = task_id or str(uuid.uuid4())
    return effective_task_id


def _reset_per_turn_agent_state(agent: Any) -> None:
    """Reset the iteration budget at turn start."""
    agent.iteration_budget = IterationBudget(agent.max_iterations)


def _stage_turn_user_message(
    agent: Any,
    user_message: Any,
) -> dict[str, Any]:
    """Build this turn's user dict."""
    user_msg = {"role": "user", "content": user_message}
    return user_msg


def build_turn_context(
    agent: Any,
    user_message: Any,
    system_message: str | None,
    conversation_history: list[dict[str, Any]] | None,
    task_id: str | None,
    *,
    restore_or_build_system_prompt: Callable[[Any, str | None, list[dict[str, Any]] | None], None],
    sanitize_surrogates: Callable[[str], str],
) -> TurnContext:
    """Run the once-per-turn setup and return the loop's input context.

    Helpers are passed in to avoid an import cycle with ``agent.conversation_loop``."""
    if isinstance(user_message, str):
        user_message = sanitize_surrogates(user_message)

    effective_task_id = _bind_turn_identity(
        agent,
        task_id,
    )
    _reset_per_turn_agent_state(agent)

    # Copy so the caller's list is never mutated.
    messages = list(conversation_history) if conversation_history else []
    user_msg = _stage_turn_user_message(
        agent,
        user_message,
    )
    append_message(messages, user_msg)

    # System prompt is cached per session for prefix caching.
    if agent._cached_system_prompt is None:
        restore_or_build_system_prompt(agent, system_message, conversation_history)
    active_system_prompt = agent._cached_system_prompt

    return TurnContext(
        messages=messages,
        active_system_prompt=active_system_prompt,
        effective_task_id=effective_task_id,
    )


def build_api_messages(
    agent: Any,
    messages: list[dict[str, Any]],
    *,
    active_system_prompt: Any,
) -> tuple[list[dict[str, Any]], str]:
    """Build the wire copy of ``messages`` for one API call plus the effective system
    message. Returns ``(api_messages, effective_system)``.

    ``messages`` stays untouched, and the system prompt is built ONCE per session and
    replayed verbatim."""
    from mertina.agent.conversation_loop import _clone_message_for_send

    api_messages = []
    for msg in messages:
        # Structural clone, NOT msg.copy(): in-place transforms below must not reach
        # persisted history via nested containers; see _clone_message_for_send.
        api_msg = _clone_message_for_send(msg)

        # Pass reasoning back to the API for ALL assistant messages so multi-turn
        # reasoning context is preserved.
        agent._copy_reasoning_content_for_api(msg, api_msg)
        # 'reasoning' is trajectory-only (copied to 'reasoning_content' above);
        # finish_reason is rejected by strict APIs (e.g. Mistral).
        api_msg.pop("reasoning", None)
        api_msg.pop("finish_reason", None)
        api_messages.append(api_msg)

    # Final system message = cached prompt.
    effective_system = active_system_prompt or ""
    if effective_system:
        api_messages = [{"role": "system", "content": effective_system}] + api_messages  # noqa: RUF005  # upstream's expression
    return api_messages, effective_system
