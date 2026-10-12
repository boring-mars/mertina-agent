"""Tool-call validation: unknown tool names and malformed JSON arguments.

Copied from Hermes agent/turn_tool_validation.py (``validate_tool_calls``,
``_partial_exit``, ``_append_tool_error_results``) and agent/conversation_loop.py
(``_invalid_tool_name_error_content``) at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md.

Role alternation is preserved on every path: an invalid batch is answered with
tool-role error results (never a user message), and the exits close any open
tool-result tail. The retry counters live on the turn state rather than on the
agent. Tool-name auto-repair and tool-call ID deduplication are left out.
"""

import dataclasses
import json
import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from mertina_agent.agent.message_sanitization import close_interrupted_tool_sequence
from mertina_agent.agent.tool_executor import make_tool_result_message
from mertina_agent.agent.transports.types import ChatMessage, NormalizedResponse, ToolCall, Usage
from mertina_agent.agent.turn_failure_copy import TRUNCATED_RESPONSE
from mertina_agent.agent.turn_response_intake import build_assistant_message
from mertina_agent.agent.turn_result import ConversationResult, partial_result

logger = logging.getLogger(__name__)

MAX_INVALID_TOOL_RETRIES = 3
"""Consecutive all-invalid tool batches tolerated before the turn ends as partial."""

MAX_INVALID_JSON_RETRIES = 3
"""Consecutive malformed-argument responses before error results are injected."""


def _invalid_tool_name_error_content(name: str, valid_tool_names: Collection[str]) -> str:
    """Error content for an unknown tool name, listing the catalog so the model can self-correct.

    Hermes also has a terse variant for a blank name; the transport's
    ``ToolCall`` contract already rejects blank names, so it is not needed here.
    """
    available = ", ".join(sorted(valid_tool_names))
    return f"Tool '{name}' does not exist. Available tools: {available}"


@dataclass
class ToolValidationVerdict:
    """Outcome of :func:`validate_tool_calls`.

    Attributes:
        action: ``"ok"`` dispatches the calls; ``"continue"`` re-issues the API
            call (error results or retry state were recorded); ``"return"`` ends
            the turn with ``result``.
        tool_calls: The calls with empty arguments normalized to ``"{}"``.
        mixed_invalid_batch: The batch holds both valid and unknown tool names;
            only the unknown ones get error results, the valid ones run.
        invalid_tool_retries: Updated count of consecutive all-invalid batches.
        invalid_json_retries: Updated count of consecutive malformed-JSON responses.
        result: The turn result when ``action`` is ``"return"``.
    """

    action: Literal["ok", "continue", "return"]
    tool_calls: tuple[ToolCall, ...]
    mixed_invalid_batch: bool
    invalid_tool_retries: int
    invalid_json_retries: int
    result: ConversationResult | None = None


def _preview_name(name: str) -> str:
    return name[:80] + "..." if len(name) > 80 else name


def _append_tool_error_results(
    messages: list[ChatMessage],
    tool_calls: Sequence[ToolCall],
    content_for: Callable[[ToolCall], str],
) -> None:
    """Append one tool-role result per call so every tool call keeps a matching result."""
    for tool_call in tool_calls:
        messages.append(
            make_tool_result_message(tool_call.name, content_for(tool_call), tool_call.id)
        )


def _partial_exit(
    messages: list[ChatMessage], api_call_count: int, final_response: str, *, usage: Usage
) -> ConversationResult:
    """Terminal partial result.

    Earlier retries or an earlier tool batch can leave a tool-result tail; close
    it as interrupt aborts do, so the next turn is not ``tool -> user``.
    """
    close_interrupted_tool_sequence(messages, final_response)
    return partial_result(
        messages,
        api_call_count,
        final_response,
        usage=usage,
        turn_exit_reason="invalid_tool_calls",
    )


def validate_tool_calls(
    response: NormalizedResponse,
    *,
    valid_tool_names: Collection[str],
    messages: list[ChatMessage],
    api_call_count: int,
    invalid_tool_retries: int,
    invalid_json_retries: int,
    usage: Usage,
) -> ToolValidationVerdict:
    """Validate the response's tool calls before any of them runs.

    Strikes for unknown names advance only when a batch has no valid call, so a
    degenerate model still halts after three. Arguments cut off mid-stream (a
    router may rewrite ``length`` to ``tool_calls``) are refused outright rather
    than retried.
    """
    # Empty arguments are a common model quirk for tools without parameters.
    tool_calls = tuple(
        dataclasses.replace(call, arguments="{}") if not call.arguments.strip() else call
        for call in response.tool_calls
    )
    mixed_invalid_batch = False

    def _verdict(
        action: Literal["ok", "continue", "return"], result: ConversationResult | None = None
    ) -> ToolValidationVerdict:
        return ToolValidationVerdict(
            action=action,
            tool_calls=tool_calls,
            mixed_invalid_batch=mixed_invalid_batch,
            invalid_tool_retries=invalid_tool_retries,
            invalid_json_retries=invalid_json_retries,
            result=result,
        )

    invalid_tool_calls = [call.name for call in tool_calls if call.name not in valid_tool_names]
    mixed_invalid_batch = bool(invalid_tool_calls) and any(
        call.name in valid_tool_names for call in tool_calls
    )
    if mixed_invalid_batch:
        invalid_tool_retries = 0
        logger.warning(
            "Unknown tool '%s' in batch; erroring that call and executing the valid ones",
            _preview_name(invalid_tool_calls[0]),
        )
    elif invalid_tool_calls:
        invalid_tool_retries += 1
        invalid_preview = _preview_name(invalid_tool_calls[0])
        logger.warning(
            "Unknown tool '%s'; sending error to model for self-correction (%d/%d)",
            invalid_preview,
            invalid_tool_retries,
            MAX_INVALID_TOOL_RETRIES,
        )
        if invalid_tool_retries >= MAX_INVALID_TOOL_RETRIES:
            invalid_tool_retries = 0
            return _verdict(
                "return",
                _partial_exit(
                    messages,
                    api_call_count,
                    f"Model generated invalid tool call: {invalid_preview}",
                    usage=usage,
                ),
            )
        messages.append(build_assistant_message(response, tool_calls))
        _append_tool_error_results(
            messages,
            tool_calls,
            lambda call: _invalid_tool_name_error_content(call.name, valid_tool_names),
        )
        return _verdict("continue")
    else:
        invalid_tool_retries = 0

    invalid_json_args: list[tuple[str, str]] = []
    for call in tool_calls:
        try:
            json.loads(call.arguments)
        except json.JSONDecodeError as exc:
            # An unknown-name call in a mixed batch never executes (it gets an
            # error result), so its broken arguments must not retry the turn.
            if not (mixed_invalid_batch and call.name not in valid_tool_names):
                invalid_json_args.append((call.name, str(exc)))

    if invalid_json_args:
        invalid_names = {name for name, _ in invalid_json_args}
        truncated = any(
            not call.arguments.rstrip().endswith(("}", "]"))
            for call in tool_calls
            if call.name in invalid_names
        )
        if truncated:
            logger.warning(
                "Truncated tool call arguments detected (finish_reason=%r); refusing to execute",
                response.finish_reason,
            )
            invalid_json_retries = 0
            return _verdict(
                "return", _partial_exit(messages, api_call_count, TRUNCATED_RESPONSE, usage=usage)
            )

        invalid_json_retries += 1
        tool_name, error_message = invalid_json_args[0]
        logger.warning("Invalid JSON in tool call arguments for '%s': %s", tool_name, error_message)
        if invalid_json_retries < MAX_INVALID_JSON_RETRIES:
            # Nothing is recorded: the same request is simply issued again.
            return _verdict("continue")

        # Instead of ending the turn, inject tool error results so the model can
        # recover; tool results (not user messages) preserve role alternation.
        logger.warning("Injecting recovery tool results for invalid JSON")
        invalid_json_retries = 0
        messages.append(build_assistant_message(response, tool_calls))

        def _json_error_result(call: ToolCall) -> str:
            if call.name not in invalid_names:
                return "Skipped: other tool call in this response had invalid JSON."
            error = next(error for name, error in invalid_json_args if name == call.name)
            return (
                f"Error: Invalid JSON arguments. {error}. "
                "For tools with no required parameters, use an empty object: {}. "
                "Please retry with valid JSON."
            )

        _append_tool_error_results(messages, tool_calls, _json_error_result)
        return _verdict("continue")

    invalid_json_retries = 0
    return _verdict("ok")
