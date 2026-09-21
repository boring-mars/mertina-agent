"""Response intake: turn a normalized model response into loop decisions.

Adapted from Hermes agent/turn_response_check.py (``check_api_response``
finish-reason routing), agent/turn_response_intake.py
(``normalize_model_response``), agent/turn_truncation.py
(``handle_content_policy_refusal``) and agent/chat_completion_helpers.py
(``build_assistant_message``) at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md.

The second-phase transport already validates the response shape, so only the
finish-reason routing remains. Length continuation, fallback providers,
scratchpad retries and request hooks are left out: a truncated response ends
the turn, and truncated tool calls are never executed.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from mertina_agent.agent.transports.types import (
    AssistantMessage,
    ChatMessage,
    NormalizedResponse,
    ToolCall,
    ToolCallMessage,
    Usage,
)
from mertina_agent.agent.turn_failure_copy import TRUNCATED_RESPONSE, content_policy_copy
from mertina_agent.agent.turn_result import ConversationResult, partial_result

logger = logging.getLogger(__name__)


def build_assistant_message(
    response: NormalizedResponse, tool_calls: Sequence[ToolCall]
) -> AssistantMessage:
    """Build the assistant history message for ``response``.

    ``tool_calls`` is passed separately because validation may normalize them
    (empty arguments become ``"{}"``). Unlike Hermes, no ``finish_reason`` or
    reasoning keys are stored: the transport accepts only protocol fields.
    """
    message: AssistantMessage = {"role": "assistant", "content": response.content}
    if tool_calls:
        message["tool_calls"] = [
            ToolCallMessage(
                id=call.id,
                type="function",
                function={"name": call.name, "arguments": call.arguments},
            )
            for call in tool_calls
        ]
    if response.refusal is not None:
        message["refusal"] = response.refusal
    return message


@dataclass
class ResponseIntakeVerdict:
    """Verdict of :func:`normalize_model_response`.

    Attributes:
        action: ``"fallthrough"`` processes the response; ``"return"`` ends the
            turn with ``result``.
        result: The turn result when ``action`` is ``"return"``.
    """

    action: Literal["fallthrough", "return"]
    result: ConversationResult | None = None


def normalize_model_response(
    response: NormalizedResponse,
    *,
    messages: list[ChatMessage],
    api_call_count: int,
    usage: Usage,
) -> ResponseIntakeVerdict:
    """Route a response by its finish reason before tools or the final answer run.

    ``content_filter`` ends the turn as a failure, as Hermes does once no
    fallback provider is left. ``length`` ends it as partial: truncated text is
    kept as the answer, while truncated tool calls are refused and not recorded.
    """
    finish_reason = response.finish_reason
    if finish_reason == "content_filter":
        refusal_text = response.refusal or response.content
        error_detail = refusal_text or "model declined (content_filter)"
        logger.warning("Model declined to respond (finish_reason=content_filter)")
        return ResponseIntakeVerdict(
            "return",
            partial_result(
                messages,
                api_call_count,
                "⚠️ "
                + content_policy_copy(summary=refusal_text or "the model returned no explanation"),
                usage=usage,
                turn_exit_reason="content_policy_blocked",
                error=f"content_policy_blocked: {error_detail}",
                failed=True,
            ),
        )

    if finish_reason == "length":
        if response.tool_calls or not (response.content and response.content.strip()):
            logger.warning(
                "Truncated response (finish_reason=length, %d tool call(s)); refusing to execute",
                len(response.tool_calls),
            )
            return ResponseIntakeVerdict(
                "return",
                partial_result(
                    messages,
                    api_call_count,
                    TRUNCATED_RESPONSE,
                    usage=usage,
                    turn_exit_reason="length_truncated",
                ),
            )
        logger.warning("Response truncated by the output length limit; returning it as partial")
        messages.append(build_assistant_message(response, ()))
        return ResponseIntakeVerdict(
            "return",
            partial_result(
                messages,
                api_call_count,
                response.content,
                usage=usage,
                turn_exit_reason="length_truncated",
                error="Response truncated by the output length limit",
            ),
        )
    return ResponseIntakeVerdict("fallthrough")
