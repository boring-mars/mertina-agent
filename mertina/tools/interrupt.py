# Ported from hermes-agent tools/interrupt.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Per-thread interrupt signaling for all tools: thread-scoped so interrupting one
agent session does not kill tools in other sessions (the gateway runs many agents in one
process). The agent passes its execution thread id to set_interrupt(); tools call
is_interrupted(), which checks the CURRENT thread."""

import contextvars
import logging
import threading

from mertina.utils import env_var_enabled

logger = logging.getLogger(__name__)

# Opt-in debug tracing — pairs with MERTINA_DEBUG_INTERRUPT in tools/environments/base.py.
_DEBUG_INTERRUPT = env_var_enabled("MERTINA_DEBUG_INTERRUPT")
if _DEBUG_INTERRUPT:
    # AIAgent's quiet_mode forces the `tools` logger to ERROR on CLI startup;
    # force ours back to INFO so the trace is visible in agent.log.
    logger.setLevel(logging.INFO)

# Interrupted thread idents + optional user-safe cause (never the user's message text).
_interrupted_threads: set[int] = set()
_interrupt_reasons: dict[int, str] = {}
_lock = threading.Lock()
# Tool-worker tid a deadline worker acts for. ``run_bounded_sync`` runs its worker under
# ``contextvars.copy_context()``, so a guard chain moved onto that worker still honours
# ``/stop`` aimed at the tool thread that spawned it (``is_interrupted`` checks both).
acting_for_tid: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "mertina_interrupt_acting_for_tid",
    default=None,
)


def set_interrupt(active: bool, thread_id: int | None = None, *, reason: str | None = None) -> None:
    """Set or clear the interrupt for *thread_id* (default: current thread); ``reason`` is
    an optional user-safe cause."""
    tid = thread_id if thread_id is not None else threading.current_thread().ident
    with _lock:
        (_interrupted_threads.add if active else _interrupted_threads.discard)(tid)  # type: ignore[arg-type]  # a running thread always has an ident
        if active and reason:
            _interrupt_reasons[tid] = reason  # type: ignore[index]  # a running thread always has an ident
        else:
            _interrupt_reasons.pop(tid, None)  # type: ignore[arg-type]  # a running thread always has an ident
        _snapshot = set(_interrupted_threads) if _DEBUG_INTERRUPT else None
    if _DEBUG_INTERRUPT:
        logger.info(
            "[interrupt-debug] set_interrupt(active=%s, target_tid=%s) "
            "called_from_tid=%s current_set=%s",
            active,
            tid,
            threading.current_thread().ident,
            _snapshot,
        )


def is_interrupted() -> bool:
    return is_thread_interrupted(threading.current_thread().ident) or is_thread_interrupted(
        acting_for_tid.get()
    )


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


def get_interrupt_reason() -> str | None:
    """User-safe interrupt cause for the current thread, if known."""
    with _lock:
        return _interrupt_reasons.get(threading.current_thread().ident)  # type: ignore[arg-type]  # a running thread always has an ident
