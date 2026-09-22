# Ported from hermes-agent agent/interrupt_control.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Interrupt control surface for ``AIAgent``: soft interrupt requests.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""

import logging

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("mertina.run_agent")

# ``interrupt()`` categories that mean a human stopped the turn.
_REASON_NEW_MESSAGE = "user sent a new message"
_REASON_USER_INTERRUPT = "user interrupt"


class InterruptControlMixin:
    """interrupt()/clear_interrupt() (see module docstring)."""

    # Set by AIAgent; declared here so the mixin type-checks on its own.
    quiet_mode: bool

    def interrupt(
        self,
        message: str | None = None,
    ) -> bool:
        """Request the agent to interrupt its current tool-calling loop (call from another
        thread)."""
        tool_interrupt_reason = _REASON_NEW_MESSAGE if message else _REASON_USER_INTERRUPT

        def _publish_interrupt_state() -> None:
            self._interrupt_requested = True
            self._interrupt_message = message
            # The turn record and the log must agree on WHO asked for the stop (#112647).
            logger.info(
                "Interrupt requested (%s): %s",
                "soft",
                tool_interrupt_reason,
            )

        _publish_interrupt_state()
        if not self.quiet_mode:
            print(
                "\n⚡ Interrupt requested"
                + (
                    f": '{message[:40]}...'"
                    if message and len(message) > 40
                    else f": '{message}'"
                    if message
                    else ""
                )
            )
        return True

    def clear_interrupt(
        self,
    ) -> bool:
        """Clear the interrupt request."""
        self._interrupt_requested = False
        self._interrupt_message = None
        return True
