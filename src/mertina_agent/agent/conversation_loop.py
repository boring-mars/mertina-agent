"""The agent conversation loop: one user turn of model calls and tool rounds.

Copied from Hermes agent/conversation_loop.py (``run_conversation``,
``_run_conversation_turn``, ``_LoopState``, ``_close_durable_failed_turn``) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

The phase pipeline is Hermes's, in Hermes's order: begin the iteration, build
the request, call the model, route the response, then run a tool round or
finish with text; ``finalize_turn`` handles every loop exit. Hermes threads
state through phases by reflecting over ``_LoopState``; here each phase takes
explicit arguments and returns a verdict whose fields the loop copies back.

The loop is asynchronous. The v0.2 gateway will run turns as tasks on its event
loop rather than handing them to a thread pool.

At this checkpoint a failed model request ends the turn; retries, interrupting
an in-flight request and outer-loop error recovery are added later.
"""

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

from mertina_agent.agent.events import EventCallback
from mertina_agent.agent.iteration_budget import IterationBudget
from mertina_agent.agent.model_client import ModelClientProtocol
from mertina_agent.agent.transports.types import ChatMessage, ToolDefinition, Usage
from mertina_agent.agent.turn_api_call import perform_api_call
from mertina_agent.agent.turn_context import build_api_messages, build_turn_context
from mertina_agent.agent.turn_failure_copy import failed_turn_notice
from mertina_agent.agent.turn_final_response import finish_text_response
from mertina_agent.agent.turn_finalizer import finalize_turn
from mertina_agent.agent.turn_iteration_prep import begin_iteration
from mertina_agent.agent.turn_response_intake import normalize_model_response
from mertina_agent.agent.turn_result import EMPTY_USAGE, ConversationResult, add_usage
from mertina_agent.agent.turn_tool_round import run_tool_round
from mertina_agent.exceptions import ModelRequestError, ModelResponseError
from mertina_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MODEL_REQUEST_FAILED = "The model request failed: {detail}"


@dataclass
class _LoopState:
    """Every local the turn loop threads through the phase modules."""

    messages: list[ChatMessage]
    current_turn_user_idx: int
    api_call_count: int = 0
    final_response: str | None = None
    interrupted: bool = False
    failed: bool = False
    turn_exit_reason: str = "unknown"
    usage: Usage = field(default=EMPTY_USAGE)
    invalid_tool_retries: int = 0
    invalid_json_retries: int = 0


async def _run_conversation_turn(
    user_message: str,
    conversation_history: list[ChatMessage] | None,
    *,
    model_client: ModelClientProtocol,
    active_system_prompt: str,
    tools: Sequence[ToolDefinition],
    valid_tool_names: Collection[str],
    tool_registry: ToolRegistry,
    max_iterations: int,
    event_callback: EventCallback | None,
    turn_id: str,
) -> tuple[ConversationResult, int]:
    """Run a complete turn with tool calling until it ends.

    Returns:
        The turn result and the index of this turn's user message in it.
    """
    context = build_turn_context(
        user_message,
        conversation_history,
        active_system_prompt=active_system_prompt,
        turn_id=turn_id,
    )
    s = _LoopState(messages=context.messages, current_turn_user_idx=context.current_turn_user_idx)
    iteration_budget = IterationBudget(max_iterations)

    while s.api_call_count < max_iterations and iteration_budget.remaining > 0:
        start = begin_iteration(
            iteration_budget=iteration_budget,
            api_call_count=s.api_call_count,
            interrupted=s.interrupted,
            turn_exit_reason=s.turn_exit_reason,
        )
        s.api_call_count = start.api_call_count
        s.interrupted = start.interrupted
        s.turn_exit_reason = start.turn_exit_reason
        if start.action == "break":
            break

        api_messages = build_api_messages(s.messages, active_system_prompt=active_system_prompt)
        try:
            response = await perform_api_call(model_client, api_messages, tools)
        except (ModelRequestError, ModelResponseError) as exc:
            logger.warning("Model request #%d failed: %s", s.api_call_count, exc)
            return {
                "final_response": MODEL_REQUEST_FAILED.format(detail=exc),
                "messages": s.messages,
                "api_calls": s.api_call_count,
                "completed": False,
                "failed": True,
                "interrupted": False,
                "partial": False,
                "turn_exit_reason": "model_request_failed",
                "usage": s.usage,
                "error": str(exc),
            }, s.current_turn_user_idx
        s.usage = add_usage(s.usage, response.usage)

        intake = normalize_model_response(
            response, messages=s.messages, api_call_count=s.api_call_count, usage=s.usage
        )
        if intake.action == "return" and intake.result is not None:
            return intake.result, s.current_turn_user_idx

        if response.tool_calls:
            round_verdict = await run_tool_round(
                response,
                messages=s.messages,
                valid_tool_names=valid_tool_names,
                tool_registry=tool_registry,
                event_callback=event_callback,
                api_call_count=s.api_call_count,
                invalid_tool_retries=s.invalid_tool_retries,
                invalid_json_retries=s.invalid_json_retries,
                usage=s.usage,
            )
            s.invalid_tool_retries = round_verdict.invalid_tool_retries
            s.invalid_json_retries = round_verdict.invalid_json_retries
            if round_verdict.action == "return" and round_verdict.result is not None:
                return round_verdict.result, s.current_turn_user_idx
            continue

        final = finish_text_response(response, messages=s.messages, api_call_count=s.api_call_count)
        s.final_response = final.final_response
        s.turn_exit_reason = final.turn_exit_reason
        break

    result = await finalize_turn(
        final_response=s.final_response,
        api_call_count=s.api_call_count,
        interrupted=s.interrupted,
        failed=s.failed,
        messages=s.messages,
        turn_exit_reason=s.turn_exit_reason,
        usage=s.usage,
        iteration_budget=iteration_budget,
        max_iterations=max_iterations,
        model_client=model_client,
        active_system_prompt=active_system_prompt,
    )
    return result, s.current_turn_user_idx


async def run_conversation(
    user_message: str,
    conversation_history: list[ChatMessage] | None = None,
    *,
    model_client: ModelClientProtocol,
    active_system_prompt: str,
    tools: Sequence[ToolDefinition],
    valid_tool_names: Collection[str],
    tool_registry: ToolRegistry,
    max_iterations: int,
    event_callback: EventCallback | None = None,
    turn_id: str,
) -> ConversationResult:
    """Run one turn and make sure its history can start the next one.

    Every result leaving the loop passes through here, so a failed turn never
    leaves the user message as the last row of the history.
    """
    result, current_turn_user_idx = await _run_conversation_turn(
        user_message,
        conversation_history,
        model_client=model_client,
        active_system_prompt=active_system_prompt,
        tools=tools,
        valid_tool_names=valid_tool_names,
        tool_registry=tool_registry,
        max_iterations=max_iterations,
        event_callback=event_callback,
        turn_id=turn_id,
    )
    _close_durable_failed_turn(result, current_turn_user_idx)
    return result


def _close_durable_failed_turn(result: ConversationResult, current_turn_user_idx: int) -> None:
    """Append an assistant boundary when an incomplete turn left no assistant tail.

    Without it, the caller's next message would follow as ``user -> user``, and
    the failed request would be silently merged into the new one. Hermes only
    closes a ``user`` tail; a request failing after a tool round leaves a
    ``tool`` tail, which is closed the same way so no history ends on a tool
    result. The notice never claims "not processed" if a tool may have run.
    """
    messages = result["messages"]
    if result["completed"] or not messages or messages[-1]["role"] not in {"user", "tool"}:
        return
    messages.append(
        {"role": "assistant", "content": failed_turn_notice(messages[current_turn_user_idx:])}
    )
