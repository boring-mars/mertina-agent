"""One tool-calling round of the conversation turn loop.

Copied from Hermes agent/turn_tool_round.py (``run_tool_round``) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

Order kept from Hermes: validate the calls, append the assistant tool-call
message (it keeps every emitted call, since each needs a matching result),
answer unknown calls with errors, execute the valid ones, then continue. Unlike
Hermes, which appends the unknown-call errors before the executed results, all
results here follow the model's call order. Persistence before execution,
guardrails, deduplication, compaction and display muting are left out.
"""

import logging
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from typing import Literal

from mertina_agent.agent.events import EventCallback
from mertina_agent.agent.tool_executor import (
    execute_tool_calls,
    make_tool_result_message,
)
from mertina_agent.agent.transports.types import ChatMessage, NormalizedResponse, Usage
from mertina_agent.agent.turn_response_intake import build_assistant_message
from mertina_agent.agent.turn_result import ConversationResult
from mertina_agent.agent.turn_tool_validation import (
    _invalid_tool_name_error_content,
    validate_tool_calls,
)
from mertina_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class ToolRoundVerdict:
    """Verdict of :func:`run_tool_round`.

    Attributes:
        action: ``"continue"`` makes the next API call; ``"return"`` ends the
            turn with ``result``.
        invalid_tool_retries: Updated count of consecutive all-invalid batches.
        invalid_json_retries: Updated count of consecutive malformed-JSON responses.
        result: The turn result when ``action`` is ``"return"``.
    """

    action: Literal["continue", "return"]
    invalid_tool_retries: int
    invalid_json_retries: int
    result: ConversationResult | None = None


async def run_tool_round(
    response: NormalizedResponse,
    *,
    messages: list[ChatMessage],
    valid_tool_names: Collection[str],
    tool_registry: ToolRegistry,
    event_callback: EventCallback | None,
    api_call_count: int,
    invalid_tool_retries: int,
    invalid_json_retries: int,
    usage: Usage,
) -> ToolRoundVerdict:
    """Validate and execute the response's tool calls, appending their results."""
    logger.info("Processing %d tool call(s)", len(response.tool_calls))
    validation = validate_tool_calls(
        response,
        valid_tool_names=valid_tool_names,
        messages=messages,
        api_call_count=api_call_count,
        invalid_tool_retries=invalid_tool_retries,
        invalid_json_retries=invalid_json_retries,
        usage=usage,
    )

    def _verdict(
        action: Literal["continue", "return"], result: ConversationResult | None = None
    ) -> ToolRoundVerdict:
        return ToolRoundVerdict(
            action=action,
            invalid_tool_retries=validation.invalid_tool_retries,
            invalid_json_retries=validation.invalid_json_retries,
            result=result,
        )

    if validation.action == "return":
        return _verdict("return", validation.result)
    if validation.action == "continue":
        return _verdict("continue")

    tool_calls = validation.tool_calls
    messages.append(build_assistant_message(response, tool_calls))

    valid_calls = [call for call in tool_calls if call.name in valid_tool_names]
    executed: list[ChatMessage] = []
    await execute_tool_calls(
        valid_calls,
        executed,
        enabled_tools=valid_tool_names,
        tool_registry=tool_registry,
        event_callback=event_callback,
    )
    # The executor answers each valid call exactly once, in order; interleave the
    # unknown-call errors so every result follows the model's call order.
    executed_results: Iterator[ChatMessage] = iter(executed)
    for call in tool_calls:
        if call.name in valid_tool_names:
            messages.append(next(executed_results))
        else:
            content = _invalid_tool_name_error_content(call.name, valid_tool_names)
            messages.append(make_tool_result_message(call.name, content, call.id))
    return _verdict("continue")
