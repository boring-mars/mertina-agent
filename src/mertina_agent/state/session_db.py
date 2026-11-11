"""Shared relational session operations and atomic, idempotent turn commits.

Adapted from the session/message responsibilities of Hermes SessionDB at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-gateway-sessions.md.
CAS, complete payload encoding and commit receipts implement Mertina's contract.
"""

import re
import time
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from mertina_agent.database.runtime import Database
from mertina_agent.exceptions import (
    DatabaseError,
    SessionConflictError,
    SessionNotFoundError,
    StorageError,
)
from mertina_agent.state.models import CommitReceipt, HistorySnapshot, SessionRecord, TurnCommit
from mertina_agent.state.schema import Schema
from mertina_agent.state.serialization import MAX_TURN_BYTES, decode_message, fingerprint


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        message = "Invalid session or operation identifier"
        raise StorageError(message)


def _title(value: str) -> None:
    if not isinstance(value, str) or len(value) > 200 or "\x00" in value:
        message = "Session title must be text of at most 200 characters"
        raise StorageError(message)
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        message = "Session title must contain valid Unicode"
        raise StorageError(message) from exc


def _integer(value: int, *, maximum: int = 2**63 - 1) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        message = "Invalid storage counter or timestamp"
        raise StorageError(message)


def _record(row: RowMapping) -> SessionRecord:
    record = SessionRecord(**row)
    _identifier(record.id)
    _title(record.title)
    for counter in (record.history_revision, record.metadata_version, record.message_count):
        _integer(counter, maximum=2**31 - 1)
    _integer(record.created_at_us)
    _integer(record.updated_at_us)
    return record


class SessionDB:
    """Implement session business operations without exposing SQL to the Gateway."""

    def __init__(self, database: Database, *, schema: Schema | None = None) -> None:
        """Borrow the database lifetime; the caller initializes schema explicitly."""
        self.database = database
        self.schema = schema if schema is not None else Schema()

    async def _get(self, connection: AsyncConnection, session_id: str) -> SessionRecord:
        _identifier(session_id)
        table = self.schema.sessions
        row = (
            (await connection.execute(select(table).where(table.c.id == session_id)))
            .mappings()
            .one_or_none()
        )
        if row is None:
            message = "Session does not exist"
            raise SessionNotFoundError(message)
        return _record(row)

    async def create_session(self, title: str = "New conversation") -> SessionRecord:
        """Create a new application-generated session identity and empty history."""
        _title(title)
        identifier = uuid4().hex
        now = time.time_ns() // 1000
        async with self.database.transaction() as connection:
            await connection.execute(
                self.schema.sessions.insert().values(
                    id=identifier,
                    title=title,
                    created_at_us=now,
                    updated_at_us=now,
                    history_revision=0,
                    metadata_version=0,
                    message_count=0,
                )
            )
            return await self._get(connection, identifier)

    async def get_session(self, session_id: str) -> SessionRecord:
        """Return durable metadata, excluding process-local run state."""
        async with self.database.transaction(write=False) as connection:
            return await self._get(connection, session_id)

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        after: tuple[int, str] | None = None,
    ) -> tuple[SessionRecord, ...]:
        """Read a stable ascending (creation time, ID) keyset page."""
        if type(limit) is not int or not 1 <= limit <= 100:
            message = "Session page limit must be between 1 and 100"
            raise StorageError(message)
        table = self.schema.sessions
        query = select(table)
        if after is not None:
            timestamp, identifier = after
            _integer(timestamp)
            _identifier(identifier)
            query = query.where(
                or_(
                    table.c.created_at_us > timestamp,
                    and_(table.c.created_at_us == timestamp, table.c.id > identifier),
                )
            )
        query = query.order_by(table.c.created_at_us, table.c.id).limit(limit)
        async with self.database.transaction(write=False) as connection:
            return tuple(_record(row) for row in (await connection.execute(query)).mappings())

    async def rename_session(
        self,
        session_id: str,
        title: str,
        *,
        expected_version: int,
    ) -> SessionRecord:
        """Change only title metadata using optimistic concurrency."""
        _title(title)
        _integer(expected_version, maximum=2**31 - 2)
        table = self.schema.sessions
        async with self.database.transaction() as connection:
            await self._get(connection, session_id)
            result = await connection.execute(
                update(table)
                .where(
                    table.c.id == session_id,
                    table.c.metadata_version == expected_version,
                )
                .values(title=title, metadata_version=expected_version + 1)
            )
            if result.rowcount != 1:
                message = "Session metadata version changed"
                raise SessionConflictError(message)
            return await self._get(connection, session_id)

    async def delete_session(self, session_id: str) -> None:
        """Atomically delete metadata, messages and receipts; caller holds the lease."""
        async with self.database.transaction() as connection:
            await self._get(connection, session_id)
            await connection.execute(
                delete(self.schema.sessions).where(self.schema.sessions.c.id == session_id)
            )

    async def load_history(self, session_id: str) -> HistorySnapshot:
        """Load a complete committed prefix and reject missing or corrupt records."""
        table = self.schema.messages
        async with self.database.transaction(write=False) as connection:
            session = await self._get(connection, session_id)
            rows = (
                await connection.execute(
                    select(table.c.seq, table.c.payload)
                    .where(
                        table.c.session_id == session_id,
                        table.c.seq <= session.message_count,
                    )
                    .order_by(table.c.seq)
                )
            ).all()
            if len(rows) != session.message_count or any(
                row.seq != index for index, row in enumerate(rows, 1)
            ):
                message = "Stored history is incomplete"
                raise StorageError(message)
            return HistorySnapshot(session, tuple(decode_message(row.payload) for row in rows))

    async def read_messages(
        self,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 50,
        through: int | None = None,
    ) -> tuple[tuple[str, ...], int]:
        """Return encoded messages bounded by an explicit, reusable high water mark."""
        _integer(after, maximum=2**31 - 1)
        if through is not None:
            _integer(through, maximum=2**31 - 1)
        if type(limit) is not int or not 1 <= limit <= 100:
            message = "Invalid message page"
            raise StorageError(message)
        table = self.schema.messages
        async with self.database.transaction(write=False) as connection:
            session = await self._get(connection, session_id)
            end = session.message_count if through is None else through
            if not 0 <= after <= end <= session.message_count:
                message = "Invalid message high water mark"
                raise StorageError(message)
            rows = (
                await connection.execute(
                    select(table.c.seq, table.c.payload)
                    .where(
                        table.c.session_id == session_id,
                        table.c.seq > after,
                        table.c.seq <= end,
                    )
                    .order_by(table.c.seq)
                    .limit(limit)
                )
            ).all()
            if [row.seq for row in rows] != list(range(after + 1, min(after + limit, end) + 1)):
                message = "Stored message page is incomplete"
                raise StorageError(message)
            for row in rows:
                decode_message(row.payload)
            return tuple(row.payload for row in rows), end

    async def _receipt(
        self,
        connection: AsyncConnection,
        session_id: str,
        operation_id: str,
    ) -> CommitReceipt | None:
        table = self.schema.commits
        row = (
            (
                await connection.execute(
                    select(table).where(
                        table.c.session_id == session_id,
                        table.c.operation_id == operation_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        receipt = CommitReceipt(**row)
        if (
            not isinstance(receipt.fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt.fingerprint)
            or receipt.revision < 1
            or not 1 <= receipt.first_seq <= receipt.last_seq
            or receipt.status not in {"completed", "stopped", "failed", "partial"}
        ):
            message = "Stored commit receipt is corrupt"
            raise StorageError(message)
        return receipt

    async def get_commit_receipt(self, session_id: str, operation_id: str) -> CommitReceipt | None:
        """Reconcile a write using the original operation identity."""
        _identifier(session_id)
        _identifier(operation_id)
        async with self.database.transaction(write=False) as connection:
            return await self._receipt(connection, session_id, operation_id)

    @staticmethod
    def _match(receipt: CommitReceipt, expected: str) -> CommitReceipt:
        if receipt.fingerprint != expected:
            message = "Operation ID was already used with different content"
            raise SessionConflictError(message)
        return receipt

    async def commit_turn(self, command: TurnCommit) -> CommitReceipt:
        """Append exactly once using a revision CAS and durable receipt in one transaction."""
        _identifier(command.session_id)
        _identifier(command.operation_id)
        _integer(command.expected_revision, maximum=2**31 - 2)
        _integer(command.expected_count, maximum=2**31 - 1)
        _integer(command.timestamp_us)
        if (
            not isinstance(command.payloads, tuple)
            or not command.payloads
            or command.expected_count + len(command.payloads) > 2**31 - 1
            or command.status not in {"completed", "stopped", "failed", "partial"}
        ):
            message = "Invalid turn commit"
            raise StorageError(message)
        for payload in command.payloads:
            decode_message(payload)
        if sum(len(item.encode("utf-8")) for item in command.payloads) > MAX_TURN_BYTES:
            message = "Turn exceeds the storage size limit"
            raise StorageError(message)
        digest = fingerprint(command)
        receipt = CommitReceipt(
            command.session_id,
            command.operation_id,
            digest,
            command.expected_revision + 1,
            command.expected_count + 1,
            command.expected_count + len(command.payloads),
            command.status,
        )
        try:
            async with self.database.transaction() as connection:
                existing = await self._receipt(connection, command.session_id, command.operation_id)
                if existing is not None:
                    return self._match(existing, digest)
                table = self.schema.sessions
                result = await connection.execute(
                    update(table)
                    .where(
                        table.c.id == command.session_id,
                        table.c.history_revision == command.expected_revision,
                        table.c.message_count == command.expected_count,
                    )
                    .values(
                        history_revision=receipt.revision,
                        message_count=receipt.last_seq,
                        updated_at_us=command.timestamp_us,
                    )
                )
                if result.rowcount != 1:
                    await self._get(connection, command.session_id)
                    message = "Session history version changed"
                    raise SessionConflictError(message)
                await connection.execute(
                    self.schema.messages.insert(),
                    [
                        {
                            "session_id": command.session_id,
                            "seq": receipt.first_seq + index,
                            "payload": payload,
                        }
                        for index, payload in enumerate(command.payloads)
                    ],
                )
                await connection.execute(
                    self.schema.commits.insert().values(
                        session_id=receipt.session_id,
                        operation_id=receipt.operation_id,
                        fingerprint=digest,
                        revision=receipt.revision,
                        first_seq=receipt.first_seq,
                        last_seq=receipt.last_seq,
                        status=receipt.status,
                    )
                )
            return receipt
        except (SessionConflictError, DatabaseError) as exc:
            # The competing transaction must have ended before we read its receipt.
            if isinstance(exc, DatabaseError) and exc.kind != "constraint":
                raise
            existing = await self.get_commit_receipt(command.session_id, command.operation_id)
            if existing is not None:
                return self._match(existing, digest)
            raise
