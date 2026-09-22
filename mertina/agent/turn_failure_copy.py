# Ported from hermes-agent agent/turn_failure_copy.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""User-facing copy for terminal turn outcomes: the iteration-summary failure text."""

from __future__ import annotations

from typing import Any

# One-off outcome strings: deterministic loop exits that are NOT failure codes.
_ONE_OFF_COPY: dict[str, str] = {
    "max_iterations_no_summary": (
        "I ran out of steps for this turn ({limit} tool calls) before finishing, and couldn't "
        "produce a summary. Send `continue` to keep going, or raise `max_iterations` in your config."  # noqa: E501  # upstream's message
    ),
}


_SITE_COPY: dict[str, str] = {**_ONE_OFF_COPY}


def site_copy(code: str, **fields: Any) -> str:
    """Chat copy for a failure code or one-off loop outcome; unknown fields default to empty
    strings."""
    return _SITE_COPY[code].format_map(_Defaults(fields))


class _Defaults(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""
