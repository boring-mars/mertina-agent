# Ported from hermes-agent agent/stream_delivery.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Streaming delivery for ``AIAgent``.

Delta fan-out and the record of streamed assistant text.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""


class StreamDeliveryMixin:
    """Delta fan-out (see module docstring)."""

    @staticmethod
    def _call_quietly(cb, *args) -> bool:
        """Call ``cb(*args)`` if set, swallowing errors; True when it ran without raising."""
        if cb is None:
            return False
        try:
            cb(*args)
            return True
        except Exception:
            return False

    def _deliver_to_stream_callbacks(self, text: str) -> bool:
        """Send ``text`` to the display + TTS delta callbacks; True if at least one accepted it."""
        results = [
            self._call_quietly(cb, text)
            for cb in (self.stream_delta_callback, self._stream_callback)
        ]
        return any(results)

    def _reset_stream_delivery_tracking(self) -> None:
        """Reset tracking for text delivered during the current model response.

        Flushes the think scrubber's benign tail first.
        """
        think_scrubber = getattr(self, "_stream_think_scrubber", None)

        def deliver(tail: str) -> None:
            if tail:
                self._deliver_to_stream_callbacks(tail)
                self._record_streamed_assistant_text(tail)

        # Flush any benign partial-tag tail held by the think scrubber first (#17924): an innocent '<' at
        # the end of the stream that turned out not to be a tag prefix should reach the UI.
        # Suppress reasoning/thinking blocks via the stateful scrubber (#17924). Earlier versions ran
        # _strip_think_blocks per-delta here, which destroyed downstream state machines when a tag was split
        # across deltas (e.g. MiniMax-M2.7 sends '<think>' and its content as separate deltas — regex case 2
        # erased the first delta, so the CLI/gateway state machine never saw the open tag and leaked the
        # reasoning content as regular response text).
        if think_scrubber is not None:
            think_tail = think_scrubber.flush()
            deliver(think_tail)
        self._current_streamed_assistant_text = ""

    @property
    def _current_streamed_assistant_text(self) -> str:
        """Visible assistant text streamed so far this turn. Backed by a list of pieces: ``+=`` on a string
        attribute copies the whole reply on every delta (quadratic). Hot-path emptiness checks look at
        ``_streamed_assistant_text_parts`` so they do not join per token."""
        parts = getattr(self, "_streamed_assistant_text_parts", None)
        return "".join(parts) if parts else ""

    @_current_streamed_assistant_text.setter
    def _current_streamed_assistant_text(self, value: str) -> None:
        self._streamed_assistant_text_parts = [value] if value else []

    def _record_streamed_assistant_text(self, text: str) -> None:
        """Accumulate visible assistant text emitted through stream callbacks."""
        if isinstance(text, str) and text:
            parts = getattr(self, "_streamed_assistant_text_parts", None)
            if parts is None:
                parts = self._streamed_assistant_text_parts = []
            parts.append(text)

    def _fire_stream_delta(self, text: str) -> None:
        """Fire all registered stream delta callbacks (display + TTS)."""
        # One paragraph break before the first text delta after a tool iteration, without
        # stacking blank lines across back-to-back tool iterations.
        prepended_break = bool(
            getattr(self, "_stream_needs_break", False) and text and text.strip()
        )
        if prepended_break:
            self._stream_needs_break = False
            text = "\n\n" + text
        if isinstance(text, str):
            # Stateful scrubber: per-delta regex stripping destroyed downstream state machines when a
            # tag was split across deltas. Legacy callers lack the scrubber attribute and get the
            # whole-string fallback.
            think_scrubber = getattr(self, "_stream_think_scrubber", None)
            text = (
                think_scrubber.feed(text)
                if think_scrubber is not None
                else self._strip_think_blocks(text)
            )
            # Only strip leading newlines on the first delta — mid-stream "\n" is legitimate markdown.
            # Check the parts list, not the joined property (joining per token copies the whole reply).
            if not prepended_break and not getattr(self, "_streamed_assistant_text_parts", None):
                text = text.lstrip("\n")
        if not text:
            return
        delivered = self._deliver_to_stream_callbacks(text)
        if delivered:
            self._record_streamed_assistant_text(text)

    def _fire_tool_gen_started(self, tool_name: str) -> None:
        """Notify the display layer that the model is generating tool call arguments (spinner for large payloads)."""
        self._call_quietly(self.tool_gen_callback, tool_name)

    def _has_stream_consumers(self) -> bool:
        """Return True if any streaming consumer is registered."""
        return (
            self.stream_delta_callback is not None
            or getattr(self, "_stream_callback", None) is not None
        )
