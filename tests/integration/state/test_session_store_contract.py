"""The same durable session contract runs on SQLite, PostgreSQL and MySQL."""

import asyncio
import inspect
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, delete, event, select, update

from mertina_agent.database.runtime import open_database
from mertina_agent.exceptions import (
    DatabaseError,
    SessionConflictError,
    SessionNotFoundError,
    StorageError,
)
from mertina_agent.state.serialization import (
    build_commit,
    decode_message,
    encode_message,
    fingerprint,
)
from mertina_agent.state.session_db import SessionDB

pytestmark = pytest.mark.integration


async def _command(store, session_id, *, messages=None, operation_id=None, status="completed"):
    snapshot = await store.load_history(session_id)
    suffix = messages or [
        {"role": "user", "content": "中文🐬 '\"; DROP TABLE imaginary; --"},
        {"role": "assistant", "content": "A committed answer"},
    ]
    return build_commit(
        snapshot,
        (*snapshot.messages, *suffix),
        operation_id=operation_id or uuid4().hex,
        status=status,
        timestamp_us=snapshot.session.updated_at_us + 1,
    )


def test_explicit_bootstrap_and_repeated_initialization_preserve_data(storage_factory):
    async def scenario():
        async with storage_factory(bootstrap=False) as store:
            with pytest.raises(StorageError, match="missing or incomplete"):
                await store.schema.initialize(store.database)
            await store.schema.initialize(store.database, bootstrap=True)
            session = await store.create_session("持久会话🐬")
            command = await _command(store, session.id)
            await store.commit_turn(command)
            await store.schema.initialize(store.database, bootstrap=True)
            await store.schema.initialize(store.database)
            history = await store.load_history(session.id)
            assert history.session.title == session.title
            assert history.session.message_count == 2
            assert history.messages == tuple(map(decode_message, command.payloads))

    asyncio.run(scenario())


def test_partial_schema_is_rejected_instead_of_completed_silently(storage_factory):
    async def scenario():
        async with storage_factory(bootstrap=False) as store:
            async with store.database.transaction() as connection:
                await connection.run_sync(store.schema.version.create)
            with pytest.raises(StorageError, match="incomplete"):
                await store.schema.initialize(store.database, bootstrap=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("version", [0, 2, None])
def test_unknown_or_missing_schema_version_is_rejected(storage_factory, version):
    async def scenario():
        async with storage_factory() as store:
            async with store.database.transaction() as connection:
                if version is None:
                    await connection.execute(delete(store.schema.version))
                else:
                    await connection.execute(update(store.schema.version).values(version=version))
            with pytest.raises(StorageError, match="Unsupported storage schema version"):
                await store.schema.initialize(store.database, bootstrap=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("missing_column", [False, True])
def test_schema_stamp_does_not_hide_missing_constraints_or_columns(storage_factory, missing_column):
    async def scenario():
        async with storage_factory() as store:
            columns = [
                Column("session_id", String(32), primary_key=True),
                Column("seq", Integer, primary_key=True, autoincrement=False),
            ]
            if not missing_column:
                columns.append(Column("payload", Text, nullable=False))
            broken = Table(store.schema.messages.name, MetaData(), *columns)
            async with store.database.transaction() as connection:
                await connection.run_sync(store.schema.messages.drop)
                await connection.run_sync(broken.create)
            with pytest.raises(StorageError, match="structure differs"):
                await store.schema.initialize(store.database)

    asyncio.run(scenario())


def test_crud_keyset_pagination_metadata_cas_and_cascading_delete(storage_factory):
    async def scenario():
        async with storage_factory() as store:
            sessions = [await store.create_session(f"Title {index} 🐬") for index in range(5)]
            async with store.database.transaction() as connection:
                await connection.execute(update(store.schema.sessions).values(created_at_us=100))
            ordered_ids = sorted(session.id for session in sessions)
            first = await store.list_sessions(limit=2)
            second = await store.list_sessions(limit=2, after=(100, first[-1].id))
            third = await store.list_sessions(limit=2, after=(100, second[-1].id))
            assert [session.id for session in (*first, *second, *third)] == ordered_ids
            session = first[0]
            command = await _command(store, session.id)
            receipt = await store.commit_turn(command)
            renamed = await store.rename_session(session.id, "New title 中文", expected_version=0)
            assert renamed.metadata_version == 1
            assert renamed.history_revision == receipt.revision
            assert renamed.message_count == 2
            assert renamed.title == "New title 中文"
            with pytest.raises(SessionConflictError, match="metadata version"):
                await store.rename_session(session.id, "Lost update", expected_version=0)
            await store.delete_session(session.id)
            with pytest.raises(SessionNotFoundError):
                await store.get_session(session.id)
            with pytest.raises(SessionNotFoundError):
                await store.delete_session(session.id)
            assert await store.get_commit_receipt(session.id, command.operation_id) is None
            async with store.database.transaction(write=False) as connection:
                assert not (await connection.execute(select(store.schema.messages))).all()

    asyncio.run(scenario())


def test_paginated_messages_keep_the_original_high_water_mark(storage_factory):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            command = await _command(store, session.id)
            await store.commit_turn(command)
            first, through = await store.read_messages(session.id, limit=1)
            assert through == 2
            await store.commit_turn(await _command(store, session.id))
            second, same_through = await store.read_messages(
                session.id, after=1, limit=1, through=through
            )
            assert same_through == through
            assert (*first, *second) == command.payloads
            assert await store.read_messages(session.id, after=2, through=through) == ((), 2)
            assert (await store.load_history(session.id)).session.message_count == 4
            with pytest.raises(StorageError, match="high water"):
                await store.read_messages(session.id, after=3, through=2)
            with pytest.raises(StorageError, match="high water"):
                await store.read_messages(session.id, through=5)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["completed", "stopped", "failed", "partial"])
def test_reopened_database_restores_tools_refusal_and_partial_turns(
    storage_factory, state_settings, status
):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            messages = [
                {"role": "user", "content": "Use a tool"},
                {
                    "role": "assistant",
                    "content": None,
                    "refusal": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": ' {"q":"\\u4e2d"} '},
                        }
                    ],
                },
                {"role": "tool", "content": '{"data":"中文🐬"}', "tool_call_id": "call-1"},
                {"role": "assistant", "refusal": "Refusal preserved", "content": None},
                {"role": "user", "content": "Try again"},
                {"role": "assistant", "content": "An answer stopped part-way"},
            ]
            command = await _command(store, session.id, messages=messages, status=status)
            receipt = await store.commit_turn(command)
            await store.database.aclose()
            async with await open_database(state_settings) as reopened:
                restored = SessionDB(reopened, schema=store.schema)
                await restored.schema.initialize(reopened)
                snapshot = await restored.load_history(session.id)
                assert snapshot.messages == tuple(messages)
                assert (
                    await restored.get_commit_receipt(session.id, command.operation_id) == receipt
                )
                assert await restored.commit_turn(command) == receipt
                assert (await restored.load_history(session.id)).session.message_count == len(
                    messages
                )

    asyncio.run(scenario())


def test_long_messages_exceeding_mysql_text_capacity_remain_lossless(storage_factory):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            messages = [{"role": "user", "content": "a" * 100_000 + "中文🐬"}]
            await store.commit_turn(await _command(store, session.id, messages=messages))
            assert (await store.load_history(session.id)).messages == tuple(messages)

    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["gap", "invalid_payload", "invalid_count"])
def test_history_and_message_pages_fail_closed_on_corruption(storage_factory, corruption):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            await store.commit_turn(await _command(store, session.id))
            table = store.schema.messages
            async with store.database.transaction() as connection:
                if corruption == "gap":
                    await connection.execute(delete(table).where(table.c.seq == 1))
                elif corruption == "invalid_payload":
                    await connection.execute(
                        update(table).where(table.c.seq == 1).values(payload="[]")
                    )
                else:
                    await connection.execute(update(store.schema.sessions).values(message_count=-1))
            with pytest.raises(StorageError):
                await store.load_history(session.id)
            with pytest.raises(StorageError):
                await store.read_messages(session.id)

    asyncio.run(scenario())


def test_atomic_turn_rolls_back_metadata_messages_and_receipt_on_insert_failure(storage_factory):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            command = await _command(store, session.id)

            def fail_receipt(_connection, _cursor, statement, _parameters, _context, _executemany):
                if statement.startswith("INSERT INTO " + store.schema.commits.name):
                    message = "Injected receipt write failure"
                    raise RuntimeError(message)

            event.listen(store.database.engine.sync_engine, "before_cursor_execute", fail_receipt)
            try:
                with pytest.raises(RuntimeError, match="Injected receipt"):
                    await store.commit_turn(command)
            finally:
                event.remove(
                    store.database.engine.sync_engine, "before_cursor_execute", fail_receipt
                )
            snapshot = await store.load_history(session.id)
            assert snapshot.session == session
            assert snapshot.messages == ()
            assert await store.get_commit_receipt(session.id, command.operation_id) is None
            assert (await store.commit_turn(command)).last_seq == 2

    asyncio.run(scenario())


def test_duplicate_operation_is_idempotent_and_mismatched_reuse_is_rejected(storage_factory):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            command = await _command(store, session.id)
            receipts = await asyncio.gather(*(store.commit_turn(command) for _ in range(6)))
            assert all(receipt == receipts[0] for receipt in receipts)
            assert (await store.load_history(session.id)).session.message_count == 2
            assert receipts[0].fingerprint == fingerprint(command)
            for different in (
                replace(command, status="stopped"),
                replace(command, timestamp_us=command.timestamp_us + 1),
                replace(command, payloads=(encode_message({"role": "user", "content": "new"}),)),
            ):
                with pytest.raises(SessionConflictError, match="different content"):
                    await store.commit_turn(different)
            assert (await store.load_history(session.id)).session.history_revision == 1

    asyncio.run(scenario())


def test_revision_cas_allows_only_one_concurrent_command(storage_factory):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            first = await _command(store, session.id)
            second = replace(first, operation_id=uuid4().hex)
            results = await asyncio.gather(
                store.commit_turn(first), store.commit_turn(second), return_exceptions=True
            )
            assert sum(isinstance(result, SessionConflictError) for result in results) == 1
            snapshot = await store.load_history(session.id)
            assert snapshot.session.message_count == 2
            assert snapshot.session.history_revision == 1

    asyncio.run(scenario())


def test_lost_commit_acknowledgement_is_reconciled_by_stable_receipt(storage_factory, monkeypatch):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            command = await _command(store, session.id)
            transaction = store.database.transaction
            lose_reply = True

            @asynccontextmanager
            async def uncertain_transaction(*, write=True):
                nonlocal lose_reply
                async with transaction(write=write) as connection:
                    yield connection
                if write and lose_reply:
                    lose_reply = False
                    raise DatabaseError(kind="unknown")

            monkeypatch.setattr(store.database, "transaction", uncertain_transaction)
            with pytest.raises(DatabaseError) as error:
                await store.commit_turn(command)
            assert error.value.kind == "unknown"
            receipt = await store.get_commit_receipt(session.id, command.operation_id)
            assert receipt is not None
            assert receipt.fingerprint == fingerprint(command)
            assert await store.commit_turn(command) == receipt
            assert (await store.load_history(session.id)).session.message_count == 2

    asyncio.run(scenario())


def test_uncommitted_failure_has_no_receipt_and_original_command_can_retry(storage_factory):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            command = await _command(store, session.id)

            def fail_commit(_connection):
                message = "Injected transport failure before driver commit"
                raise OSError(message)

            event.listen(store.database.engine.sync_engine, "commit", fail_commit)
            try:
                with pytest.raises(DatabaseError) as error:
                    await store.commit_turn(command)
                assert error.value.kind == "unknown"
            finally:
                event.remove(store.database.engine.sync_engine, "commit", fail_commit)
            assert await store.get_commit_receipt(session.id, command.operation_id) is None
            assert (await store.load_history(session.id)).messages == ()
            assert (await store.commit_turn(command)).revision == 1

    asyncio.run(scenario())


def test_database_constraints_reject_orphans_and_duplicate_message_numbers(storage_factory):
    async def scenario():
        async with storage_factory() as store:
            payload = encode_message({"role": "user", "content": "text"})
            with pytest.raises(DatabaseError) as error:
                async with store.database.transaction() as connection:
                    await connection.execute(
                        store.schema.messages.insert().values(
                            session_id="f" * 32, seq=1, payload=payload
                        )
                    )
            assert error.value.kind == "constraint"
            session = await store.create_session()
            await store.commit_turn(await _command(store, session.id))
            with pytest.raises(DatabaseError) as error:
                async with store.database.transaction() as connection:
                    await connection.execute(
                        store.schema.messages.insert().values(
                            session_id=session.id, seq=1, payload=payload
                        )
                    )
            assert error.value.kind == "constraint"
            assert (await store.load_history(session.id)).session.message_count == 2

    asyncio.run(scenario())


def test_stale_pooled_connection_is_replaced_without_losing_committed_history(storage_factory):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            command = await _command(store, session.id)
            receipt = await store.commit_turn(command)
            async with store.database.transaction(write=False) as connection:
                raw = await connection.get_raw_connection()
                stale_driver = raw.driver_connection
            # Close the physical driver after check-in, leaving a stale pool entry.
            # This tests pre-ping rather than explicitly invalidating the pool.
            closed = stale_driver.close()
            if inspect.isawaitable(closed):
                await closed
            assert (await store.load_history(session.id)).messages == tuple(
                map(decode_message, command.payloads)
            )
            assert await store.get_commit_receipt(session.id, command.operation_id) == receipt
            async with store.database.transaction(write=False) as connection:
                fresh = await connection.get_raw_connection()
                assert fresh.driver_connection is not stale_driver

    asyncio.run(scenario())


def test_cancellation_after_driver_commit_keeps_receipt_for_reconciliation(
    storage_factory, monkeypatch
):
    async def scenario():
        async with storage_factory() as store:
            session = await store.create_session()
            command = await _command(store, session.id)
            committed = asyncio.Event()
            never = asyncio.Event()
            dialect = store.database.engine.sync_engine.dialect
            commit = dialect.do_commit

            async def delay_acknowledgement(_driver):
                committed.set()
                await never.wait()

            def commit_then_wait(connection):
                commit(connection)
                connection.run_async(delay_acknowledgement)

            monkeypatch.setattr(dialect, "do_commit", commit_then_wait)
            task = asyncio.create_task(store.commit_turn(command))
            try:
                await asyncio.wait_for(committed.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                monkeypatch.setattr(dialect, "do_commit", commit)
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            receipt = await store.get_commit_receipt(session.id, command.operation_id)
            assert receipt is not None
            assert receipt.fingerprint == fingerprint(command)
            assert await store.commit_turn(command) == receipt
            assert (await store.load_history(session.id)).session.message_count == 2

    asyncio.run(scenario())
