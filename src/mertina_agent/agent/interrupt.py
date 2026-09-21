"""Run-scoped interrupt signalling shared by the agent loop and the tools it calls.

Adapted from Hermes tools/interrupt.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md for the pinned source and reductions.

Hermes keys interrupts by thread ident because each agent turn owns a thread.
Mertina runs turns as asyncio tasks, so the signal travels in a context variable
instead. ``asyncio.to_thread`` copies the context, which keeps
:func:`is_interrupted` working inside synchronous tool handlers without any
change to how tools call it.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class InterruptSignal:
    """A stop request for one agent run, safe to set from any thread.

    A stop request is not an error: it only asks the loop and cooperative tools
    to stop starting new work. What happens to work already in flight is decided
    by the caller that observes the signal.
    """

    def __init__(self) -> None:
        """Create a signal that is not set."""
        self._flag = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None

    def set(self, reason: str | None = None) -> None:
        """Request a stop.

        Args:
            reason: Optional user-safe cause. It may reach tool output, so it
                must never carry the user's message text.
        """
        with self._lock:
            self._reason = reason
            self._flag.set()

    def clear(self) -> None:
        """Withdraw the stop request and forget its reason."""
        with self._lock:
            self._reason = None
            self._flag.clear()

    def is_set(self) -> bool:
        """Return whether a stop has been requested."""
        return self._flag.is_set()

    @property
    def reason(self) -> str | None:
        """The user-safe cause given with the current stop request, if any."""
        with self._lock:
            return self._reason if self._flag.is_set() else None


_current_signal: ContextVar[InterruptSignal | None] = ContextVar(
    "mertina_interrupt_signal", default=None
)


@contextmanager
def bind_interrupt_signal(signal: InterruptSignal) -> Iterator[InterruptSignal]:
    """Make ``signal`` the one observed by :func:`is_interrupted` in this context.

    The previous binding is restored on exit, including when the body raises or
    the enclosing task is cancelled.
    """
    token = _current_signal.set(signal)
    try:
        yield signal
    finally:
        _current_signal.reset(token)


def is_interrupted() -> bool:
    """Return whether the run that owns the current context was asked to stop.

    Code running outside any agent run is never interrupted.
    """
    signal = _current_signal.get()
    return signal is not None and signal.is_set()


def get_interrupt_reason() -> str | None:
    """Return the user-safe cause of the current run's stop request, if known."""
    signal = _current_signal.get()
    return signal.reason if signal is not None else None
