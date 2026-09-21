# Ported from hermes-agent agent/display.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""CLI presentation -- spinner, kawaii faces, tool preview formatting.

Pure display functions with no AIAgent dependency; used for CLI feedback.
"""

from typing import Any

from mertina.agent.tool_result_classification import (
    file_mutation_result_landed,
    is_guardrail_refusal,
)
from mertina.utils import safe_json_loads


def _tail_trunc(text: str, limit: int | None) -> str:
    """Tail-truncate to ``limit`` chars with ``...`` (0/None = unlimited). The result never
    exceeds ``limit``: for 1-3 the ellipsis itself is clipped (``text[:limit - 3]`` would go
    negative and hand back almost the whole string, #9439)."""
    if not limit or limit <= 0 or len(text) <= limit:
        return text
    return "." * limit if limit <= 3 else text[: limit - 3] + "..."


_ERROR_SUFFIX_MAX_LEN = 48


# A degraded backend (Docker down, SSH host unreachable) needs the whole reason plus the fix hint.
_DEGRADED_SUFFIX_MAX_LEN = 200


def _trim_error(msg: str) -> str:
    """Shrink an error message for inline display (long 'File not found' paths -> filename)."""
    msg = msg.strip()
    if "File not found:" in msg:
        tail = msg.partition("File not found:")[2].strip()
        if "/" in tail:
            msg = f"File not found: {tail.rsplit('/', 1)[-1]}"
    return _tail_trunc(msg, _ERROR_SUFFIX_MAX_LEN)


def _degraded_suffix(data: dict) -> str:
    """`` [<reason> — <retry_hint>]`` for a ``status: degraded`` terminal result (hint omitted when empty)."""
    reason = str(data.get("reason") or data.get("error") or "terminal backend unavailable").strip()
    hint = str(data.get("retry_hint") or "").strip()
    text = f"{reason} — {hint}" if hint else reason
    return f" [{_tail_trunc(text, _DEGRADED_SUFFIX_MAX_LEN)}]"


def _detect_tool_failure(tool_name: str, result: Any) -> tuple[bool, str]:
    """Return ``(is_failure, suffix)`` for a tool result, e.g. ``(True, " [exit 1]")``."""
    if result is None or file_mutation_result_landed(tool_name, result):
        return False, ""
    data = result if isinstance(result, dict) else safe_json_loads(result)
    # A harness REFUSAL of a redundant call (repeated identical read/search) is not a
    # failed call. This is the ``failed`` the executor hands the loop guardrail, so
    # counting it would escalate refusals into ``repeated_exact_failure_block``.
    if is_guardrail_refusal(data):
        return False, ""

    # A denied/timed-out approval carries one human sentence; show it instead of the model-facing
    # "BLOCKED: ... Do NOT retry" text (which stays in the JSON for the model).
    if isinstance(data, dict) and data.get("user_summary"):
        return True, f" [{_tail_trunc(str(data['user_summary']), _DEGRADED_SUFFIX_MAX_LEN)}]"

    # Terminal: non-zero exit code is the canonical failure signal.
    if tool_name == "terminal":
        exit_code = data.get("exit_code") if isinstance(data, dict) else None
        if exit_code is None or exit_code == 0:
            return False, ""
        if data.get("status") == "degraded":
            return True, _degraded_suffix(data)
        err_msg = data.get("error")
        return True, f" [{_trim_error(str(err_msg))}]" if err_msg else f" [exit {exit_code}]"

    if isinstance(data, dict):
        failed = data.get("success") is False
        # Memory: distinguish "store full" from real errors.
        if tool_name == "memory" and failed and "exceed the limit" in data.get("error", ""):
            return True, " [full]"
        err = data.get("error") or data.get("message")
        if err and (failed or "error" in data):
            return True, f" [{_trim_error(str(err))}]"
    # Multimodal results (dicts) are successes; failures arrive as JSON-encoded strings.
    if isinstance(result, str) and (
        '"error"' in result[:500].lower()
        or '"failed"' in result[:500].lower()
        or result.startswith("Error")
    ):
        return True, " [error]"
    return False, ""
