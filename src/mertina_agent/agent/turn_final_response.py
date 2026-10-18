"""No-tool-call branch of the conversation turn loop: record the final answer.

Copied from Hermes agent/turn_final_response.py (``finish_text_response``) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

The transport already rejects responses with neither text, tool calls nor a
refusal, so Hermes's empty-response ladder, stall/degenerate/acknowledgement
continuations, dropped-tool-call nudge, stop gates and reasoning promotion are
left out.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from mertina_agent.agent.transports.types import ChatMessage, NormalizedResponse
from mertina_agent.agent.turn_response_intake import build_assistant_message

logger = logging.getLogger(__name__)


@dataclass
class FinalResponseVerdict:
    """Verdict of :func:`finish_text_response`.

    Attributes:
        action: Always ``"break"``: the turn ends with ``final_response``.
        final_response: Text delivered to the user; a refusal when there is no text.
        turn_exit_reason: ``text_response(finish_reason=...)``.
    """

    action: Literal["break"]
    final_response: str
    turn_exit_reason: str


def finish_text_response(
    response: NormalizedResponse, *, messages: list[ChatMessage], api_call_count: int
) -> FinalResponseVerdict:
    """Append the final assistant message and end the turn."""
    final_response = response.content or response.refusal or ""
    messages.append(build_assistant_message(response, ()))
    logger.info("Conversation completed after %d model call(s)", api_call_count)
    return FinalResponseVerdict(
        action="break",
        final_response=final_response,
        turn_exit_reason=f"text_response(finish_reason={response.finish_reason})",
    )
