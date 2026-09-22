"""User-facing text for turns that end without a normal answer.

Copied from Hermes agent/turn_failure_copy.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md for the pinned source and reductions.

Only the entries v0.1 can reach are kept. References to Hermes slash commands,
config keys and the product name are replaced with Mertina's equivalents.
"""

from collections.abc import Sequence

from mertina_agent.agent.transports.types import ChatMessage

FAILED_TURN_NOTICE = (
    "Your request was not processed. Send it again if you still want me to carry it out."
)
PARTIAL_FAILED_TURN_NOTICE = (
    "This turn did not complete. Some actions may already have run; verify their effects "
    "before resending."
)

CONTENT_POLICY_NEXT_STEPS = (
    "Try rewording your message or removing sensitive content, or switch to another model."
)

TRUNCATED_RESPONSE = (
    "The model's reply was cut off before it finished (it hit its output length limit), so "
    "the agent didn't run the incomplete action. Nothing was changed. Send `continue`, or ask "
    "for the work in smaller steps."
)

MAX_ITERATIONS_NO_SUMMARY = (
    "I ran out of steps for this turn ({limit} model calls) before finishing, and couldn't "
    "produce a summary. Send `continue` to keep going, or raise MERTINA_MAX_ITERATIONS."
)

MODEL_REQUEST_FAILED = "The model request failed: {detail}"

RETRIES_EXHAUSTED = "The model request failed after {attempts} attempts: {detail}"

RATE_LIMITED_EXHAUSTED = (
    "The model provider is rate limiting requests and still refused after {attempts} attempts: "
    "{detail}. Wait a moment and send your message again."
)

NEXT_STEPS_LOOP = "Your message is saved. Send `continue` to try again."

LOCAL_PROCESSING_ERROR = (
    "The agent hit an internal error while handling the model's reply and stopped this turn. "
    + NEXT_STEPS_LOOP
    + "\n\nDetails: {detail}"
)

EMPTY_SUMMARY_RESPONSE = "I reached the iteration limit and couldn't generate a summary."


def failed_turn_notice(turn_messages: Sequence[ChatMessage]) -> str:
    """Boundary text for a failed turn; never claims "not processed" if a tool may have run."""
    for row in turn_messages:
        if row["role"] == "tool" or (row["role"] == "assistant" and row.get("tool_calls")):
            return PARTIAL_FAILED_TURN_NOTICE
    return FAILED_TURN_NOTICE


def content_policy_copy(*, summary: str) -> str:
    """Final response for a provider safety refusal (``finish_reason=content_filter``)."""
    return (
        "The model provider's safety filter refused this request, so the model didn't answer. "
        f"{CONTENT_POLICY_NEXT_STEPS}\n\nProvider said: {summary}"
    )
