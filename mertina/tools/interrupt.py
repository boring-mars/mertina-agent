# Ported from hermes-agent tools/interrupt.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Per-thread interrupt signaling for all tools: thread-scoped so interrupting one
agent session does not kill tools in other sessions (the gateway runs many agents in one
process). The agent passes its execution thread id to set_interrupt(); tools call
is_interrupted(), which checks the CURRENT thread."""

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

# Interrupted thread idents.
_interrupted_threads: set[int] = set()
_lock = threading.Lock()


def set_interrupt(active: bool, thread_id: int | None = None) -> None:
    """Set or clear the interrupt for *thread_id* (default: current thread)."""
    tid = thread_id if thread_id is not None else threading.current_thread().ident
    with _lock:
        (_interrupted_threads.add if active else _interrupted_threads.discard)(tid)  # type: ignore[arg-type]  # a running thread always has an ident
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
    return is_thread_interrupted(threading.current_thread().ident)


def is_thread_interrupted(thread_id: int | None) -> bool:
    """Whether *thread_id* has an interrupt bit set (``None`` never is)."""
    if thread_id is None:
        return False
    with _lock:
        return thread_id in _interrupted_threads
