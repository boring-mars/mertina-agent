"""Serialize each session's history-load, execution and durable-commit region.

Adapted from Hermes gateway/turn_lease.py at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt.

Keep identity-checked release and atomic holder publication; remove waiters,
rotation aliases and idle entries. P0 rejects contention immediately. This is
process-local coordination for one event loop, not a distributed lock.
"""

from mertina_agent.exceptions import SessionConflictError


class Lease:
    """An identity-bound ownership token returned by the registry.

    Release is synchronous and idempotent. Callers must release in ``finally``
    after terminal-state publication, including failed and cancelled runs.
    """

    __slots__ = ("_owner_id", "_registry", "_released", "_session_id")

    def __init__(
        self, registry: "SessionTurnLeaseRegistry", session_id: str, owner_id: str
    ) -> None:
        """Create a token; acquire ownership through the registry instead."""
        self._registry = registry
        self._session_id = session_id
        self._owner_id = owner_id
        self._released = False

    @property
    def session_id(self) -> str:
        """Return the resolved session protected by this token."""
        return self._session_id

    @property
    def owner_id(self) -> str:
        """Return the run or maintenance operation that acquired this token."""
        return self._owner_id

    @property
    def released(self) -> bool:
        """Report whether release has already been requested."""
        return self._released

    def release(self) -> None:
        """Release only this exact holder, leaving any newer lease untouched."""
        if self._released:
            return
        self._released = True
        self._registry._release(self)


class SessionTurnLeaseRegistry:
    """Fail-fast leases used only on the Gateway's single event loop.

    Acquisition and release never await, so checking and publishing ownership
    cannot interleave with another request. Only active owners are retained;
    the run registry separately limits the number of active runs.
    """

    def __init__(self) -> None:
        """Create an empty registry without owning any external resources."""
        self._leases: dict[str, Lease] = {}

    def acquire(self, session_id: str, owner_id: str) -> Lease:
        """Acquire before reading history, or fail without waiting.

        Raises:
            SessionConflictError: If another operation owns the session.
        """
        if session_id in self._leases:
            message = "Session has an active operation"
            raise SessionConflictError(message)
        lease = Lease(self, session_id, owner_id)
        self._leases[session_id] = lease
        return lease

    def active_owner(self, session_id: str) -> str | None:
        """Return the current owner's ID, or None for an idle session."""
        lease = self._leases.get(session_id)
        return lease.owner_id if lease is not None else None

    def _release(self, lease: Lease) -> None:
        if self._leases.get(lease.session_id) is lease:
            del self._leases[lease.session_id]
