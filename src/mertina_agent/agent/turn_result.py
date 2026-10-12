"""The result of one conversation turn, and the helpers that build it.

Hermes returns a plain dict assembled in several phase modules
(``finalize_turn``, ``partial_result`` in agent/turn_truncation.py,
``_content_policy_blocked_result`` in agent/conversation_loop.py, all at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b). Mertina keeps Hermes's key names in a
``TypedDict`` and gathers the constructors here, so phase modules can build
results without importing the loop module. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt.
"""

from typing import NotRequired, TypedDict

from mertina_agent.agent.transports.types import ChatMessage, Usage


class ConversationResult(TypedDict):
    """What :meth:`Agent.run_conversation` returns.

    ``messages`` is the complete history including this turn and can be passed
    back unchanged as the next turn's ``conversation_history``.

    Attributes:
        final_response: Text delivered to the user, or ``None`` if there is none.
        messages: Full history after the turn.
        api_calls: Model requests made during the turn.
        completed: Whether the turn ended with a normal answer.
        failed: Whether the turn ended because of an error.
        interrupted: Whether the turn was stopped.
        partial: Whether the turn ended early without failing, e.g. on truncation.
        turn_exit_reason: Diagnostic reason the loop ended.
        usage: Token usage summed over the turn; a count is ``None`` once any
            response left it unreported.
        error: Short description of what went wrong, when anything did.
    """

    final_response: str | None
    messages: list[ChatMessage]
    api_calls: int
    completed: bool
    failed: bool
    interrupted: bool
    partial: bool
    turn_exit_reason: str
    usage: Usage
    error: NotRequired[str]


EMPTY_USAGE = Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
"""Usage of a turn before its first model call: a measured zero."""


def add_usage(total: Usage, reported: Usage | None) -> Usage:
    """Add one response's usage to a running total without inventing counts.

    An unknown count is not zero: once any response omits a count, the total
    for that count stays unknown.
    """
    if reported is None:
        return Usage()

    def _sum(left: int | None, right: int | None) -> int | None:
        return None if left is None or right is None else left + right

    return Usage(
        prompt_tokens=_sum(total.prompt_tokens, reported.prompt_tokens),
        completion_tokens=_sum(total.completion_tokens, reported.completion_tokens),
        total_tokens=_sum(total.total_tokens, reported.total_tokens),
    )


def partial_result(
    messages: list[ChatMessage],
    api_call_count: int,
    final_response: str,
    *,
    usage: Usage,
    turn_exit_reason: str,
    error: str | None = None,
    failed: bool = False,
) -> ConversationResult:
    """Build an incomplete-turn result: ``partial`` unless ``failed``.

    ``error`` defaults to ``final_response``, as in Hermes.
    """
    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "failed": failed,
        "interrupted": False,
        "partial": not failed,
        "turn_exit_reason": turn_exit_reason,
        "usage": usage,
        "error": final_response if error is None else error,
    }
