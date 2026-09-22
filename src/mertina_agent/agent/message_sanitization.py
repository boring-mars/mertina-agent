"""History repairs that keep a transcript valid for strict providers.

Copied from Hermes agent/message_sanitization.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md for the pinned source and reductions.

Only the two helpers v0.1 needs are kept; the send-path sanitizers, argument
repair and replay canonicalization are left out.
"""

import re

from mertina_agent.agent.transports.types import ChatMessage

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD; a no-op when none are present.

    Lone surrogates cannot be encoded as UTF-8, so one in the history would make
    every later request fail to serialize.
    """
    return _SURROGATE_RE.sub("�", text)


def close_interrupted_tool_sequence(
    messages: list[ChatMessage], final_response: str | None = None
) -> bool:
    """Append a closing assistant message when the history ends on a tool result.

    A transcript ending on a raw ``tool`` message makes the next user message
    land as ``tool -> user``, an alternation that strict providers answer by
    hallucinating a continuation. Mutates ``messages`` in place.

    Returns:
        ``True`` if a closing message was appended.
    """
    if not messages or messages[-1]["role"] != "tool":
        return False
    text = (final_response or "").strip() or "Operation interrupted."
    messages.append({"role": "assistant", "content": text})
    return True
