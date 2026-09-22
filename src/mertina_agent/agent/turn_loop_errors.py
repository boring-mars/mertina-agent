"""Outer-loop exception handler for the conversation turn loop.

Copied from Hermes agent/turn_loop_errors.py (``handle_outer_loop_error``) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

In Mertina, model failures are handled by the retry loop and tool failures by
the registry, so an exception reaching this handler is a deterministic local
bug. Hermes ends the turn on those without retrying ("local processing error");
Mertina does the same for every escaped exception, which makes Hermes's
traceback-module classification, per-turn error cap and interpreter-shutdown
branch unnecessary.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from mertina_agent.agent.tool_executor import make_tool_result_message
from mertina_agent.agent.transports.types import ChatMessage
from mertina_agent.agent.turn_failure_copy import LOCAL_PROCESSING_ERROR

logger = logging.getLogger(__name__)


def short_detail(error: BaseException, limit: int = 200) -> str:
    """First line of an exception's text, capped, for a trailing ``Details:`` line."""
    lines = (str(error) or type(error).__name__).strip().splitlines()
    first = lines[0] if lines else type(error).__name__
    return first if len(first) <= limit else first[: limit - 1] + "…"


@dataclass
class OuterErrorVerdict:
    """Verdict of :func:`handle_outer_loop_error`: the turn ends as failed.

    Attributes:
        action: Always ``"break"``.
        final_response: Explanation delivered to the user.
        turn_exit_reason: ``local_processing_error(...)``.
    """

    action: Literal["break"]
    final_response: str
    turn_exit_reason: str


def handle_outer_loop_error(
    error: Exception, *, messages: list[ChatMessage], api_call_count: int
) -> OuterErrorVerdict:
    """Close unanswered tool calls with error results and end the turn.

    An appended assistant tool-call message needs one ``tool`` result per call,
    so every unanswered call of the newest batch gets an error result. The
    assistant message itself is never appended here; ``finalize_turn`` closes
    the tail.
    """
    error_message = f"Error during local message processing after API call #{api_call_count}: "
    logger.error("%s%s", error_message, type(error).__name__, exc_info=error)

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message["role"] == "tool":
            continue
        if message["role"] == "assistant" and message.get("tool_calls"):
            answered = {
                row["tool_call_id"] for row in messages[index + 1 :] if row["role"] == "tool"
            }
            for tool_call in message.get("tool_calls") or ():
                if tool_call["id"] not in answered:
                    messages.append(
                        make_tool_result_message(
                            tool_call["function"]["name"],
                            f"Error executing tool: {error_message}{short_detail(error)}",
                            tool_call["id"],
                        )
                    )
        break

    return OuterErrorVerdict(
        action="break",
        final_response=LOCAL_PROCESSING_ERROR.format(detail=short_detail(error)),
        turn_exit_reason=f"local_processing_error({type(error).__name__})",
    )
