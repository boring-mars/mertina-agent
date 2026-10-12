"""Turn prologue and per-request message assembly.

Copied from Hermes agent/turn_context.py (``TurnContext``, ``build_turn_context``,
``build_api_messages``) and agent/conversation_loop.py (``_clone_message_for_send``)
at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

The prologue keeps the order Hermes relies on: copy the caller's history,
append the user message, record its index, and reuse the system prompt built
once per agent. Session persistence, compaction, memory prefetch, plugin
context, API-content sidecars and reasoning replay are left out.
"""

import logging
from dataclasses import dataclass
from typing import cast

from mertina_agent.agent.message_sanitization import _sanitize_surrogates
from mertina_agent.agent.transports.types import ChatMessage

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    """Values produced by the turn prologue and consumed by the turn loop.

    Attributes:
        user_message: Inbound message with lone surrogates replaced.
        messages: Working history for this turn; the loop appends to it.
        active_system_prompt: System prompt sent with every request of the turn.
        turn_id: Identifier used to correlate log lines of one turn.
        current_turn_user_idx: Index of this turn's user message in ``messages``.
    """

    user_message: str
    messages: list[ChatMessage]
    active_system_prompt: str
    turn_id: str
    current_turn_user_idx: int


def _clone_json(value: object) -> object:
    if isinstance(value, dict):
        return {key: _clone_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_clone_json(item) for item in value]
    return value


def _clone_message_for_send(message: ChatMessage) -> ChatMessage:
    """Structurally clone a history message: containers copied, immutable leaves shared.

    Cheaper than ``deepcopy`` because messages are JSON-shaped and acyclic.
    Later rewrites of the copy can then never reach the caller's history.
    """
    return cast(ChatMessage, _clone_json(message))


def build_turn_context(
    user_message: str,
    conversation_history: list[ChatMessage] | None,
    *,
    active_system_prompt: str,
    turn_id: str,
) -> TurnContext:
    """Run the once-per-turn setup and return the loop's input context.

    The caller's history is cloned, so neither the list nor its messages are
    ever modified by the turn.
    """
    user_message = _sanitize_surrogates(user_message)
    logger.info(
        "Conversation turn %s: history=%d messages",
        turn_id,
        len(conversation_history or ()),
    )
    messages = [_clone_message_for_send(message) for message in conversation_history or ()]
    messages.append({"role": "user", "content": user_message})
    return TurnContext(
        user_message=user_message,
        messages=messages,
        active_system_prompt=active_system_prompt,
        turn_id=turn_id,
        current_turn_user_idx=len(messages) - 1,
    )


def build_api_messages(
    messages: list[ChatMessage], *, active_system_prompt: str
) -> list[ChatMessage]:
    """Build the wire copy of ``messages`` for one request.

    The system prompt is built once per agent and replayed verbatim, so the
    request prefix stays byte-stable across iterations and turns.
    """
    api_messages = [_clone_message_for_send(message) for message in messages]
    if active_system_prompt:
        api_messages.insert(0, {"role": "system", "content": active_system_prompt})
    return api_messages
