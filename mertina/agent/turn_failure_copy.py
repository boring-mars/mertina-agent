# Ported from hermes-agent agent/turn_failure_copy.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""User-facing copy and ``failure_reason`` stamping for terminal failed-turn results.

Every terminal result dict the turn loop returns must carry ``failure_reason`` (a
``FailoverReason`` value or one of :data:`SITE_FAILURE_CODES`) and ``failure_retryable`` so
``agent/error_surface.py`` yields a specific descriptor instead of ``unknown``. The copy
tables here say WHAT happened and WHAT TO DO in plain words; raw provider detail rides a
trailing "Provider said:" / "Details:" line.
"""

from __future__ import annotations

from typing import Any

from mertina.constants import display_hermes_home

_NEXT_STEPS_RETRY = "Wait a minute and send /retry, or switch models with /model."


_NEXT_STEPS_LOOP = (
    "Your message is saved. Send `continue` to try again, or start a new session with /new. "
    "If it happens again, run `hermes doctor` and share the error details."
)


# Chat copy for the codes in SITE_FAILURE_CODES that a loop site renders itself
# (``empty_response`` is worded by agent/turn_explainers.py, ``session_busy`` by the lease).
_FAILURE_CODE_COPY: dict[str, str] = {
    "context_overflow": (
        "This conversation has grown too long for {model} to read, and Hermes couldn't shrink "
        "it enough automatically. Start a new session with /new (your history is kept), or try "
        "/compress once more. Switching to a model with a bigger context window also works."
    ),
    "truncated": (
        "The model's reply was cut off before it finished (it hit its output length limit), so "
        "Hermes didn't run the incomplete action. Nothing was changed. Send `continue`, ask for "
        "the work in smaller steps, or raise max_tokens for this model."
    ),
    "invalid_response": (
        "{label} sent back an empty or broken reply {attempts} times — it is probably overloaded "
        "or rate-limiting you. " + _NEXT_STEPS_RETRY + "\n\nDetails: {detail}"
    ),
    "loop_error": (
        "Hermes hit repeated errors and stopped this turn so it wouldn't keep retrying. "
        + _NEXT_STEPS_LOOP
        + "\n\nDetails: {detail}"
    ),
    "interpreter_shutdown": (
        "Hermes was shutting down and stopped this turn. Your conversation is saved — reopen "
        "it{resume} and send your message again."
    ),
}


# One-off outcome strings: deterministic loop exits that are NOT failure codes (the result
# they ride carries a code from the table above, or none at all).
_ONE_OFF_COPY: dict[str, str] = {
    "payload_too_large": (
        "This conversation (including attachments) has grown too large to send to {model}, and "
        "Hermes couldn't shrink it enough automatically. Start a new session with /new (your "
        "history is kept), or try /compress once more."
    ),
    "compression_disabled": (
        "This conversation is too long for {model} and automatic shrinking is turned off in "
        "your settings (compression.enabled). Run /compress to shrink it now, /new to start "
        "fresh, or pick a model with a bigger context window."
    ),
    # Wording deliberately avoids the overflow phrases gateway/run_turn.py matches on
    # (``_CONTEXT_OVERFLOW_ERROR_PHRASES``): this failure is transient, so the user's
    # message must stay in the transcript and the session must not be auto-reset.
    "server_context_rejection": (
        "The model server rejected this request as too large, but this conversation is only "
        "about {tokens:,} tokens — well under the {window:,}-token window Hermes knows for "
        "{model} — so shrinking it would not help. Another request on the same server (for "
        "example a background memory review from an earlier session) was probably holding its "
        "capacity, or the server runs {model} with a smaller window than Hermes assumes. Wait a "
        "moment and send /retry; if it keeps happening, check the server's context setting."
    ),
    "stream_dropped_tool_call": (
        "The connection to {label} kept dropping while the model was writing a large action, "
        "so nothing was run. Check your network and send /retry; asking for the file in smaller "
        "pieces also helps."
    ),
    # Rides failure_reason="loop_error" (advisory; the turn is incomplete, not failed).
    "local_processing_error": (
        "Hermes hit an internal error while handling the model's reply and stopped this turn. "
        + _NEXT_STEPS_LOOP
        + "\n\nDetails: {detail}"
    ),
    "reasoning_only": (
        "⚠️ {model} spent all of its output budget thinking and never wrote an answer. Lower "
        "its reasoning effort with `/reasoning low`, or switch to a different model with /model. "
        "Its last thoughts, which may contain the answer:\n\n{preview}"
    ),
    "max_iterations_no_summary": (
        "I ran out of steps for this turn ({limit} tool calls) before finishing, and couldn't "
        "produce a summary. Send `continue` to keep going, or raise `max_iterations` in your config."
    ),
    "nous_rate_limit": (
        "Wait for the reset and send /retry, or switch models with /model. To avoid waits, add "
        "a backup provider with `hermes fallback add`."
    ),
}


_SITE_COPY: dict[str, str] = {**_FAILURE_CODE_COPY, **_ONE_OFF_COPY}


def site_copy(code: str, **fields: Any) -> str:
    """Chat copy for a failure code or one-off loop outcome; unknown fields default to empty strings."""
    fields.setdefault("home", display_hermes_home())
    return _SITE_COPY[code].format_map(_Defaults(fields))


class _Defaults(dict):
    def __missing__(self, key: str) -> str:
        return ""
