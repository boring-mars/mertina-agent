"""Immutable session snapshots and idempotent commit commands."""

from dataclasses import dataclass
from typing import Literal

from mertina_agent.agent.transports.types import ChatMessage

type TurnStatus = Literal["completed", "stopped", "failed", "partial"]


@dataclass(frozen=True)
class SessionRecord:
    """Durable session metadata, separate from the process-local active run."""

    id: str
    title: str
    created_at_us: int
    updated_at_us: int
    history_revision: int
    metadata_version: int
    message_count: int


@dataclass(frozen=True)
class HistorySnapshot:
    """Complete history at a single revision and message-count high water mark."""

    session: SessionRecord
    messages: tuple[ChatMessage, ...]


@dataclass(frozen=True)
class TurnCommit:
    """Stable command reused verbatim after an uncertain commit result."""

    session_id: str
    operation_id: str
    expected_revision: int
    expected_count: int
    payloads: tuple[str, ...]
    status: TurnStatus
    timestamp_us: int


@dataclass(frozen=True)
class CommitReceipt:
    """Durable acknowledgement; retained until its session is deleted."""

    session_id: str
    operation_id: str
    fingerprint: str
    revision: int
    first_seq: int
    last_seq: int
    status: str
