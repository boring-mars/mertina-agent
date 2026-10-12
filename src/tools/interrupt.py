"""Per-thread interrupt signaling for all tools: thread-scoped so interrupting one
agent session does not kill tools in other sessions (the gateway runs many agents in one
process). The agent passes its execution thread id to set_interrupt(); tools call
is_interrupted(), which checks the CURRENT thread."""

import contextvars
import logging
import threading
from collections.abc import Callable

from utils import env_var_enabled

logger = logging.getLogger(__name__)

# Opt-in debug tracing — pairs with HERMES_DEBUG_INTERRUPT in tools/environments/base.py.
_DEBUG_INTERRUPT = env_var_enabled("HERMES_DEBUG_INTERRUPT")
if _DEBUG_INTERRUPT:
    # AIAgent's quiet_mode forces the `tools` logger to ERROR on CLI startup;
    # force ours back to INFO so the trace is visible in agent.log.
    logger.setLevel(logging.INFO)

# Interrupted thread idents + optional user-safe cause (never the user's message text).
_interrupted_threads: set[int] = set()
_interrupt_reasons: dict[int, str] = {}
# Threads asked to YIELD: hand a long-running foreground command to the background
# instead of killing it, so a mid-turn user message is not parked behind it.
_yield_threads: set[int] = set()
_lock = threading.Lock()
# Tool-worker tid a deadline worker acts for. ``run_bounded_sync`` runs its worker under
# ``contextvars.copy_context()``, so a guard chain moved onto that worker still honours
# ``/stop`` aimed at the tool thread that spawned it (``is_interrupted`` checks both).
acting_for_tid: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "hermes_interrupt_acting_for_tid", default=None,
)


def set_interrupt(active: bool, thread_id: int | None = None, *, reason: str | None = None) -> None:
    """Set or clear the interrupt for *thread_id* (default: current thread); ``reason`` is
    an optional user-safe cause. Clearing also drops a pending yield request."""
    tid = thread_id if thread_id is not None else threading.current_thread().ident
    with _lock:
        (_interrupted_threads.add if active else _interrupted_threads.discard)(tid)
        if active and reason:
            _interrupt_reasons[tid] = reason
        else:
            _interrupt_reasons.pop(tid, None)
        if not active:
            _yield_threads.discard(tid)
        _snapshot = set(_interrupted_threads) if _DEBUG_INTERRUPT else None
    if _DEBUG_INTERRUPT:
        logger.info(
            "[interrupt-debug] set_interrupt(active=%s, target_tid=%s) "
            "called_from_tid=%s current_set=%s",
            active, tid, threading.current_thread().ident, _snapshot)


def is_interrupted() -> bool:
    return is_thread_interrupted(threading.current_thread().ident) or is_thread_interrupted(acting_for_tid.get())


def is_thread_interrupted(thread_id: int | None) -> bool:
    """Whether *thread_id* has an interrupt bit set (``None`` never is). Used when
    a wait moves onto a deadline worker (``run_bounded_sync``) so ``/stop``
    targeting the original tool-worker tid still kills the subprocess.

    See #94285.
    """
    if thread_id is None:
        return False
    with _lock:
        return thread_id in _interrupted_threads


def request_yield(thread_id: int) -> None:
    """Ask the tool running on *thread_id* to yield: a foreground terminal command hands
    its live process to the background registry and returns at once, so a user's mid-turn
    message (``redirect()`` during tool execution) is delivered instead of parked behind it.
    The command itself is never killed; that is what ``set_interrupt`` is for."""
    with _lock:
        _yield_threads.add(thread_id)
