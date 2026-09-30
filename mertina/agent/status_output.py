# Ported from hermes-agent agent/status_output.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""User-facing status plumbing for ``AIAgent``: safe printing and quiet-mode gating.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""

import logging
from collections.abc import Callable
from typing import Any

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("mertina.run_agent")


class StatusOutputMixin:
    """Status emission (see module docstring)."""

    # Set by AIAgent; declared here so the mixin type-checks on its own.
    _print_fn: Callable[..., Any] | None

    def _safe_print(self, *args: Any, diagnostic: bool = False, **kwargs: Any) -> None:
        """Print that swallows broken pipes / closed stdout (headless stdout can vanish
        mid-session); routes through ``self._print_fn`` so the CLI can inject an ANSI-aware
        renderer."""
        try:
            (self._print_fn or print)(*args, **kwargs)
        except (OSError, ValueError):
            pass

    def _vprint(
        self, *args: Any, force: bool = False, diagnostic: bool = False, **kwargs: Any
    ) -> None:
        """Verbose print — suppressed after the main response; ``force=True`` bypasses it.
        ``suppress_status_output`` (``mertina chat -q``) wins."""
        if getattr(self, "suppress_status_output", False):
            return
        if force or not getattr(self, "_mute_post_response", False):
            self._safe_print(*args, **kwargs)

    def _call_callback(self, name: str, *args, origin: str) -> None:
        """Invoke ``self.<name>(*args)`` if set, swallowing errors — a driver callback must never break the loop."""
        cb = getattr(self, name, None)
        if cb:
            try:
                cb(*args)
            except Exception:
                logger.debug("%s error in %s", name, origin, exc_info=True)

    def _emit_status_kind(self, kind: str, message: str, *, origin: str) -> None:
        """Print to the CLI (``_vprint(force=True)``) and forward to ``status_callback(kind, message)``. Never raises."""
        from mertina.gateway.warning_notifications import is_warning_status

        try:
            if not is_warning_status(kind, message) or self._warning_presentation_enabled():
                self._vprint(f"{self.log_prefix}{message}", force=True)
        except Exception:
            pass
        self._call_callback("status_callback", kind, message, origin=origin)

    def _warning_presentation_enabled(self) -> bool:
        from mertina.gateway.warning_notifications import warning_notifications_enabled

        try:
            return warning_notifications_enabled(
                getattr(self, "_notification_platform", getattr(self, "platform", "cli")),
                getattr(self, "_notification_config", None),
            )
        except Exception:
            # A presentation preference must never turn a recoverable notice into a failed turn.
            return True

    def _emit_diagnostic_status(self, message: str) -> None:
        """A diagnostic on the lifecycle rail, without changing legacy formatting."""
        from mertina.gateway.warning_notifications import DiagnosticText

        self._emit_status(DiagnosticText(message))

    def _emit_status(self, message: str) -> None:
        """Emit a lifecycle status message (CLI + gateway ``status_callback``)."""
        self._emit_status_kind("lifecycle", message, origin="_emit_status")

    def _emit_warning(self, message: str) -> None:
        """Emit a user-visible warning for degraded side paths where the turn continues but the user must know."""
        self._emit_status_kind("warn", message, origin="_emit_warning")

    def _emit_wait_notice(self, text: str) -> None:
        """Rewrite the live status line (CLI spinner, TUI ``thinking.delta``, gateway activity)
        so long provider waits are not an anonymous spinner."""
        self._touch_activity(text)
        self._call_callback("thinking_callback", text, origin="_emit_wait_notice")

    def _emit_diagnostic_wait(self, text: str) -> None:
        from mertina.gateway.warning_notifications import DiagnosticText

        self._emit_wait_notice(DiagnosticText(text))

    def _buffer_retry_message(self, kind: str, message: str) -> None:
        """Buffer a retry/fallback line as ``(kind, text)`` until we know whether the turn recovered.

        ``kind`` is ``"status"`` (replays via ``_emit_status``), ``"vprint"`` (``_vprint(force=True)``) or
        ``"warn"`` (``_emit_warning``).
        """
        buf = getattr(self, "_retry_status_buffer", None)
        if buf is None:
            buf = self._retry_status_buffer = []
        buf.append((kind, message))

    def _buffer_status(self, message: str) -> None:
        self._buffer_retry_message("status", message)

    def _buffer_diagnostic_status(self, message: str) -> None:
        from mertina.gateway.warning_notifications import DiagnosticText

        self._buffer_status(DiagnosticText(message))

    def _buffer_vprint(self, message: str) -> None:
        self._buffer_retry_message("vprint", message)

    def _clear_status_buffer(self) -> None:
        """Drop buffered retry messages — call on successful recovery."""
        buf = getattr(self, "_retry_status_buffer", None)
        if buf:
            buf.clear()

    def _emit_pending_fallback_notice(self) -> None:
        """Surface the one-shot fallback-switch notice on successful recovery: a provider switch is durable
        state operators must see, unlike the retry chatter ``_clear_status_buffer`` drops. Emitted once, then
        cleared; on terminal failure the buffered switch line is flushed instead (``_flush_status_buffer``)."""
        notice = getattr(self, "_pending_fallback_notice", None)
        if not notice:
            return
        # Clear before emitting so a (swallowed) callback error can't leave a stale re-emit.
        self._pending_fallback_notice = None
        for item in notice if isinstance(notice, list) else [notice]:
            try:
                self._emit_diagnostic_status(item)
            except Exception:
                # One surface failure must not hide later switches from the same chain.
                continue

    def _flush_status_buffer(self) -> None:
        """Emit buffered retry messages — call on terminal failure so the user sees what was tried."""
        # The buffered trace already carries the switch line; drop the one-shot notice.
        self._pending_fallback_notice = None
        buf = getattr(self, "_retry_status_buffer", None)
        if not buf:
            return
        # Drain first so a callback exception doesn't double-emit.
        messages = list(buf)
        buf.clear()
        replay = {"status": self._emit_status, "warn": self._emit_warning}
        for kind, msg in messages:
            try:
                if kind in replay:
                    replay[kind](msg)
                else:
                    if self._warning_presentation_enabled():
                        self._vprint(f"{self.log_prefix}{msg}", force=True)
            except Exception:
                pass
