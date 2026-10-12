"""MemoryManager — fans the agent's memory hooks out to registered providers.

The builtin provider is always allowed; only ONE external plugin provider may be
registered at a time (tool-schema bloat, conflicting backends).
"""


import re
































_FENCE_TAG_RE = re.compile(r'</?\s*memory-context\s*>', re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(r'<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>', re.IGNORECASE)
_INTERNAL_NOTE_RE = re.compile(
    r'\[System note:\s*The following is recalled memory context,\s*NOT new user input\.\s*Treat as (?:informational background data|authoritative reference data[^\]]*)\.\]\s*',
    re.IGNORECASE,
)


def sanitize_context(text: str) -> str:
    """Strip fence tags, injected context blocks, and system notes from provider output."""
    for pattern in (_INTERNAL_CONTEXT_RE, _INTERNAL_NOTE_RE, _FENCE_TAG_RE):
        text = pattern.sub('', text)
    return text


class StreamingContextScrubber:
    """Stateful scrubber for streaming text whose memory-context spans may straddle deltas.

    ``sanitize_context`` needs both tags in one string, so a split span would leak to the UI;
    this holds back partial-tag tails between ``feed()`` calls and drops span interiors.
    One scrubber (or ``reset()``) per top-level response; call ``flush()`` at end of stream.
    """

    _OPEN_TAG = "<memory-context>"
    _CLOSE_TAG = "</memory-context>"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._in_span: bool = False
        self._buf: str = ""
        self._at_block_boundary: bool = True

    def feed(self, text: str) -> str:
        """Return the visible portion of ``text``; a possible partial tag tail is held for the next call."""
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []
        while buf:
            if self._in_span:
                tag = self._CLOSE_TAG
                idx = buf.lower().find(tag)
                held = self._max_partial_suffix(buf, tag)  # potential partial close tag
            else:
                tag = self._OPEN_TAG
                idx = self._find_boundary_open_tag(buf)
                # A complete boundary tag at the buffer end is held until the next char confirms it.
                n = len(tag)
                pending = n if buf.lower().endswith(tag) and self._ends_at_block_boundary(buf[:-n]) else 0
                held = pending or self._max_partial_suffix(buf, tag)
            if idx == -1:
                # Hold back the possible partial tag; inside a span the rest is dropped.
                if not self._in_span:
                    self._append_visible(out, buf[:-held] if held else buf)
                self._buf = buf[-held:] if held else ""
                break
            if not self._in_span:
                self._append_visible(out, buf[:idx])
            buf = buf[idx + len(tag):]
            self._in_span = not self._in_span
        return "".join(out)

    def flush(self) -> str:
        """Emit the held-back tail at end-of-stream; inside an unterminated span it is discarded
        (leaking partial memory context is worse than a truncated answer)."""
        tail = "" if self._in_span else self._buf
        self._buf = ""
        self._in_span = False
        return tail

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        """Length of the longest buf-suffix that is a (case-insensitive) prefix of ``tag``, else 0."""
        tag_lower, buf_lower = tag.lower(), buf.lower()
        span = range(min(len(buf_lower), len(tag_lower) - 1), 0, -1)
        return next((i for i in span if tag_lower.startswith(buf_lower[-i:])), 0)

    def _find_boundary_open_tag(self, buf: str) -> int:
        """Find an opening fence only when it starts a block-like span (own line, newline after)."""
        buf_lower, tag_len = buf.lower(), len(self._OPEN_TAG)
        idx = buf_lower.find(self._OPEN_TAG)
        while idx != -1:
            after_idx = idx + tag_len
            if self._ends_at_block_boundary(buf[:idx]) and after_idx < len(buf) and buf[after_idx] in "\r\n":
                return idx
            idx = buf_lower.find(self._OPEN_TAG, idx + 1)
        return -1

    def _ends_at_block_boundary(self, text: str) -> bool:
        """Whether emitting ``text`` leaves the stream at a line start (blank tail after the last newline;
        no newline at all -> only whitespace and already at a boundary)."""
        head, sep, tail = text.rpartition("\n")
        return tail.strip() == "" and (bool(sep) or self._at_block_boundary)

    def _append_visible(self, out: list[str], text: str) -> None:
        if text:
            out.append(text)
            self._at_block_boundary = self._ends_at_block_boundary(text)





































































