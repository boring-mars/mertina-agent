"""Post-loop turn finalization: budget summary, transcript tail, result assembly.

Copied from Hermes agent/turn_finalizer.py (``finalize_turn``,
``_resolve_budget_fallback``, ``_close_transcript_tail``) and
agent/chat_completion_helpers.py (``handle_max_iterations``) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

Trajectory saving, persistence, micro-compaction, output hooks, memory sync,
background review and kanban bookkeeping are left out.
"""

import logging
import re
from dataclasses import dataclass

from mertina_agent.agent.iteration_budget import IterationBudget
from mertina_agent.agent.message_sanitization import (
    _sanitize_surrogates,
    close_interrupted_tool_sequence,
)
from mertina_agent.agent.model_client import ModelClientProtocol
from mertina_agent.agent.transports.types import ChatMessage, Usage
from mertina_agent.agent.turn_context import build_api_messages
from mertina_agent.agent.turn_failure_copy import (
    EMPTY_SUMMARY_RESPONSE,
    MAX_ITERATIONS_NO_SUMMARY,
)
from mertina_agent.agent.turn_result import ConversationResult, add_usage
from mertina_agent.exceptions import ModelRequestError, ModelResponseError

logger = logging.getLogger(__name__)

MAX_ITERATIONS_SUMMARY_REQUEST = (
    "You've reached the maximum number of tool-calling iterations allowed. Please provide a final "
    "response summarizing what you've found and accomplished so far, without calling any more "
    "tools."
)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


@dataclass
class _SummaryOutcome:
    final_response: str
    api_calls: int
    usage: Usage


async def handle_max_iterations(
    messages: list[ChatMessage],
    *,
    model_client: ModelClientProtocol,
    active_system_prompt: str,
    max_iterations: int,
    usage: Usage,
) -> _SummaryOutcome:
    """Ask the model for a summary once the iteration budget is spent.

    Appends the summary request as a user message and makes a toolless request,
    retried once when it comes back without text. A summary that is empty once
    its ``<think>`` block is stripped is not retried.
    """
    logger.warning("Reached maximum iterations (%d); requesting summary", max_iterations)
    messages.append({"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST})
    api_messages = build_api_messages(messages, active_system_prompt=active_system_prompt)
    api_calls = 0
    final_response = EMPTY_SUMMARY_RESPONSE
    try:
        for _attempt in range(2):
            api_calls += 1
            response = await model_client.complete(api_messages, tools=())
            usage = add_usage(usage, response.usage)
            if response.tool_calls:
                logger.warning("Iteration summary emitted tool calls; discarding them")
            text = (response.content or "").strip()
            if not text:
                continue
            text = _THINK_BLOCK_RE.sub("", text).strip() if "<think>" in text else text
            if text:
                messages.append({"role": "assistant", "content": text})
                final_response = text
            break
    except (ModelRequestError, ModelResponseError) as exc:
        logger.warning("Failed to get summary response: %s", exc)
        final_response = MAX_ITERATIONS_NO_SUMMARY.format(limit=max_iterations)
    return _SummaryOutcome(final_response=final_response, api_calls=api_calls, usage=usage)


def _close_transcript_tail(
    messages: list[ChatMessage], final_response: str | None, *, interrupted: bool
) -> None:
    """Shape the transcript tail so the history can be replayed as-is.

    An interrupt can leave a tool result as the tail; it is closed so strict
    providers do not see ``tool -> user``. Otherwise a delivered
    ``final_response`` always has a matching assistant message: recovery paths
    that return text without appending one are closed here, at the single
    point every loop exit flows through.
    """
    if interrupted:
        close_interrupted_tool_sequence(messages, final_response)
        return
    if final_response and (not messages or messages[-1]["role"] != "assistant"):
        messages.append({"role": "assistant", "content": final_response})


async def finalize_turn(
    *,
    final_response: str | None,
    api_call_count: int,
    interrupted: bool,
    failed: bool,
    messages: list[ChatMessage],
    turn_exit_reason: str,
    usage: Usage,
    iteration_budget: IterationBudget,
    max_iterations: int,
    model_client: ModelClientProtocol,
    active_system_prompt: str,
) -> ConversationResult:
    """Run the post-loop finalization and return the turn result."""
    budget_exhausted = api_call_count >= max_iterations or iteration_budget.remaining <= 0
    if (
        final_response is None
        and budget_exhausted
        and not interrupted
        and not failed
        and turn_exit_reason in {"unknown", "budget_exhausted"}
    ):
        turn_exit_reason = f"max_iterations_reached({api_call_count}/{max_iterations})"
        summary = await handle_max_iterations(
            messages,
            model_client=model_client,
            active_system_prompt=active_system_prompt,
            max_iterations=max_iterations,
            usage=usage,
        )
        final_response = summary.final_response
        api_call_count += summary.api_calls
        usage = summary.usage

    completed = (
        final_response is not None
        and not failed
        and not interrupted
        and (api_call_count < max_iterations or turn_exit_reason.startswith("text_response("))
    )
    _close_transcript_tail(messages, final_response, interrupted=interrupted)
    logger.info(
        "Turn ended: reason=%s api_calls=%d completed=%s",
        turn_exit_reason,
        api_call_count,
        completed,
    )
    return {
        # Model text leaves the loop here; a lone surrogate would crash consumers.
        "final_response": (
            _sanitize_surrogates(final_response) if final_response is not None else None
        ),
        "messages": messages,
        "api_calls": api_call_count,
        "completed": completed,
        "failed": failed,
        "interrupted": interrupted,
        "partial": False,
        "turn_exit_reason": turn_exit_reason,
        "usage": usage,
    }
