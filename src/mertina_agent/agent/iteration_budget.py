"""Per-agent iteration budget: a thread-safe consume/refund counter.

Adapted from Hermes agent/iteration_budget.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt and
docs/sources/hermes-agent-core.md for the pinned source and reductions.

The lock is only held for a counter update and never across an ``await``, so it
is safe to share between the event loop and tool worker threads.
"""

import threading


class IterationBudget:
    """Bound the number of model calls a turn may make.

    Attributes:
        max_total: Iterations the budget allows before :meth:`consume` fails.
    """

    def __init__(self, max_total: int) -> None:
        """Create an unused budget of ``max_total`` iterations."""
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to use one iteration.

        Returns:
            ``True`` if an iteration was available and is now used.
        """
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration, for a call that produced no usable result."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        """Iterations consumed so far."""
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        """Iterations still available, never negative."""
        with self._lock:
            return max(0, self.max_total - self._used)
