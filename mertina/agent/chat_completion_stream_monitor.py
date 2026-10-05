# Ported from hermes-agent agent/chat_completion_stream_monitor.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Interrupt poll of the request-local streaming monitor."""

import threading
from collections.abc import Callable
from typing import Any


class StreamingWaitMonitor:
    agent: Any
    _call_done: threading.Event
    _abort_for_interrupt: Callable[[], None]

    def _monitor_loop(self) -> None:
        while not self._call_done.is_set():
            self._call_done.wait(timeout=0.3)
            if self.agent._interrupt_requested:
                self._abort_for_interrupt()
                return
