"""Driver-independent business interface consumed by the session service."""

from typing import Protocol

from mertina_agent.state.models import CommitReceipt, HistorySnapshot, SessionRecord, TurnCommit


class SessionStore(Protocol):
    """Atomic session operations; no SQL or transaction handles cross this boundary."""

    async def create_session(self, title: str = "New conversation") -> SessionRecord:
        """Create a new conversation."""
        ...

    async def get_session(self, session_id: str) -> SessionRecord:
        """Read durable metadata."""
        ...

    async def list_sessions(
        self, *, limit: int = 50, after: tuple[int, str] | None = None
    ) -> tuple[SessionRecord, ...]:
        """Read a deterministic keyset page."""
        ...

    async def rename_session(
        self, session_id: str, title: str, *, expected_version: int
    ) -> SessionRecord:
        """Rename using a metadata revision."""
        ...

    async def delete_session(self, session_id: str) -> None:
        """Delete all records belonging to the session."""
        ...

    async def load_history(self, session_id: str) -> HistorySnapshot:
        """Read complete history at one committed revision."""
        ...

    async def read_messages(
        self, session_id: str, *, after: int = 0, limit: int = 50, through: int | None = None
    ) -> tuple[tuple[str, ...], int]:
        """Read a fixed-prefix message page."""
        ...

    async def commit_turn(self, command: TurnCommit) -> CommitReceipt:
        """Commit one immutable command atomically and idempotently."""
        ...

    async def get_commit_receipt(self, session_id: str, operation_id: str) -> CommitReceipt | None:
        """Reconcile an uncertain commit."""
        ...
