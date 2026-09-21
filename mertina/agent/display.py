# Ported from hermes-agent agent/display.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Tool-result presentation helpers: failure detection for a tool result.

Pure functions with no AIAgent dependency.
"""

from typing import Any

from mertina.utils import safe_json_loads


def _tail_trunc(text: str, limit: int | None) -> str:
    """Tail-truncate to ``limit`` chars with ``...`` (0/None = unlimited). The result never
    exceeds ``limit``: for 1-3 the ellipsis itself is clipped (``text[:limit - 3]`` would go
    negative and hand back almost the whole string, #9439)."""
    if not limit or limit <= 0 or len(text) <= limit:
        return text
    return "." * limit if limit <= 3 else text[: limit - 3] + "..."


_ERROR_SUFFIX_MAX_LEN = 48


def _trim_error(msg: str) -> str:
    """Shrink an error message for inline display (long 'File not found' paths -> filename)."""
    msg = msg.strip()
    if "File not found:" in msg:
        tail = msg.partition("File not found:")[2].strip()
        if "/" in tail:
            msg = f"File not found: {tail.rsplit('/', 1)[-1]}"
    return _tail_trunc(msg, _ERROR_SUFFIX_MAX_LEN)


def _detect_tool_failure(tool_name: str, result: Any) -> tuple[bool, str]:
    """Return ``(is_failure, suffix)`` for a tool result, e.g. ``(True, " [error]")``."""
    if result is None:
        return False, ""
    data = result if isinstance(result, dict) else safe_json_loads(result)
    if isinstance(data, dict):
        failed = data.get("success") is False
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
