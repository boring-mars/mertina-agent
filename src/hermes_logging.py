"""Centralized logging setup for Hermes Agent.

Log files: agent.log (INFO+, everything), errors.log (WARNING+), gateway.log (INFO+,
gateway components; ``mode="gateway"``), gui.log (INFO+, dashboard/TUI-gateway;
``mode="gui"``). All are rotating files driven through one async queue and formatted
with ``RedactingFormatter`` so secrets never reach disk.
"""

import logging

import threading

from hermes_constants import get_hermes_home

_session_context = threading.local()

def set_session_context(session_id: str) -> None:
    """Set the session ID for the current thread."""
    _session_context.session_id = session_id

def _install_session_record_factory() -> None:
    """Replace the global LogRecord factory with one that adds ``session_tag``.

    Unlike a Filter, the record factory runs for EVERY record in the process (propagated
    and third-party-handled ones included), so ``%(session_tag)s`` never KeyErrors.
    Idempotent via a marker attribute.
    """
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_hermes_session_injector", False):
        return

    def _session_record_factory(*args, **kwargs):
        record = current_factory(*args, **kwargs)
        sid = getattr(_session_context, "session_id", None)
        record.session_tag = f" [{sid}]" if sid else ""  # type: ignore[attr-defined]
        # QueueListener formats on its own thread, after the profile-scoped
        # ContextVar is gone; keep the resolved home on the record so a
        # multiplex desktop ticker can route to the job owner's files (#97489).
        try:
            record.hermes_home = str(get_hermes_home().resolve())  # type: ignore[attr-defined]
        except Exception:
            record.hermes_home = ""  # type: ignore[attr-defined]
        return record
    _session_record_factory._hermes_session_injector = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(_session_record_factory)

_install_session_record_factory()
