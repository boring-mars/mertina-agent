"""One real-engine contract shared by every advertised backend."""

import asyncio
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, select, text
from sqlalchemy.engine import make_url

from mertina_agent.database.runtime import open_database
from mertina_agent.exceptions import DatabaseError

pytestmark = pytest.mark.integration


def _schema():
    metadata = MetaData()
    table = Table(
        "mertina_probe_" + uuid4().hex,
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("value", String(200), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    return metadata, table


def test_commit_rollback_unicode_binding_and_reopen(database_settings):
    async def scenario():
        metadata, table = _schema()
        database = await open_database(database_settings)
        try:
            async with database.transaction() as connection:
                await connection.run_sync(metadata.create_all)
            payload = "中文 🐬 '); DROP TABLE imaginary; --"
            async with database.transaction() as connection:
                await connection.execute(table.insert().values(id=1, value=payload))

            async def fail_write():
                async with database.transaction() as connection:
                    await connection.execute(table.insert().values(id=2, value="rolled back"))
                    message = "abort"
                    raise RuntimeError(message)

            with pytest.raises(RuntimeError, match="abort"):
                await fail_write()
            with pytest.raises(DatabaseError) as error:
                async with database.transaction() as connection:
                    await connection.execute(table.insert().values(id=1, value="duplicate"))
            assert error.value.kind == "constraint"
            assert "duplicate" not in str(error.value)
            await database.aclose()
            with pytest.raises(DatabaseError) as error:
                await database.healthcheck()
            assert error.value.kind == "closed"
            database = await open_database(database_settings)
            async with database.transaction(write=False) as connection:
                assert (await connection.execute(select(table))).all() == [(1, payload)]
        finally:
            async with database.transaction() as connection:
                await connection.run_sync(metadata.drop_all)
            await database.aclose()
            await database.aclose()

    asyncio.run(scenario())


def test_cancellation_rolls_back_and_returns_connection(database_settings):
    async def scenario():
        metadata, table = _schema()
        async with await open_database(database_settings) as database:
            async with database.transaction() as connection:
                await connection.run_sync(metadata.create_all)
            entered = asyncio.Event()

            async def writer():
                async with database.transaction() as connection:
                    await connection.execute(table.insert().values(id=1, value="cancelled"))
                    entered.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(writer())
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                async with database.transaction(write=False) as connection:
                    assert (await connection.execute(select(table))).all() == []
                await database.healthcheck()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                async with database.transaction() as connection:
                    await connection.run_sync(metadata.drop_all)

    asyncio.run(scenario())


def test_transaction_deadline_rolls_back(database_settings):
    async def scenario():
        metadata, table = _schema()
        settings = database_settings.model_copy(update={"database_timeout_s": 0.25})
        async with await open_database(settings) as database:
            async with database.transaction() as connection:
                await connection.run_sync(metadata.create_all)
            try:

                async def slow_write():
                    async with database.transaction() as connection:
                        await connection.execute(table.insert().values(id=1, value="timeout"))
                        await asyncio.Event().wait()

                with pytest.raises(DatabaseError) as error:
                    await slow_write()
                assert error.value.kind == "timeout"
                async with database.transaction(write=False) as connection:
                    assert (await connection.execute(select(table))).all() == []
            finally:
                async with database.transaction() as connection:
                    await connection.run_sync(metadata.drop_all)

    asyncio.run(scenario())


def test_concurrent_transactions_remain_independent(database_settings):
    async def scenario():
        metadata, table = _schema()
        async with await open_database(database_settings) as database:
            async with database.transaction() as connection:
                await connection.run_sync(metadata.create_all)

            async def insert(number):
                async with database.transaction() as connection:
                    await connection.execute(table.insert().values(id=number, value=str(number)))

            try:
                await asyncio.gather(*(insert(number) for number in range(10)))
                async with database.transaction(write=False) as connection:
                    assert len((await connection.execute(select(table))).all()) == 10
            finally:
                async with database.transaction() as connection:
                    await connection.run_sync(metadata.drop_all)

    asyncio.run(scenario())


def test_backend_connection_settings(database_settings):
    async def scenario():
        async with (
            await open_database(database_settings) as database,
            database.transaction(write=False) as connection,
        ):
            if database.backend == "sqlite":
                assert (await connection.execute(text("PRAGMA foreign_keys"))).scalar() == 1
                assert (await connection.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
                assert (await connection.execute(text("PRAGMA synchronous"))).scalar() == 2
                assert (await connection.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
            elif database.backend == "postgresql":
                result = await connection.execute(text("SHOW transaction_isolation"))
                assert result.scalar() == "read committed"
            else:
                result = await connection.execute(
                    text("SELECT @@transaction_isolation, @@character_set_connection")
                )
                assert result.one() == ("READ-COMMITTED", "utf8mb4")

    asyncio.run(scenario())


def test_foreign_key_constraint_is_enforced(database_settings):
    async def scenario():
        metadata, parent = _schema()
        child = Table(
            parent.name + "_child",
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=False),
            Column("parent_id", Integer, ForeignKey(parent.c.id)),
            mysql_engine="InnoDB",
        )
        async with await open_database(database_settings) as database:
            async with database.transaction() as connection:
                await connection.run_sync(metadata.create_all)
            try:
                with pytest.raises(DatabaseError) as error:
                    async with database.transaction() as connection:
                        await connection.execute(child.insert().values(id=1, parent_id=999))
                assert error.value.kind == "constraint"
                await database.healthcheck()
            finally:
                async with database.transaction() as connection:
                    await connection.run_sync(metadata.drop_all)

    asyncio.run(scenario())


def test_invalid_target_does_not_fall_back(database_settings, tmp_path):
    url = make_url(database_settings.database_url.get_secret_value())
    if url.get_backend_name() == "sqlite":
        url = url.set(database=str(tmp_path))  # A directory cannot be a database file.
    else:
        url = url.set(password="deliberately-invalid-test-password")
    settings = database_settings.model_copy(
        update={"database_url": SecretStr(url.render_as_string(hide_password=False))}
    )
    with pytest.raises(DatabaseError) as error:
        asyncio.run(open_database(settings))
    assert error.value.kind == "connection"
    assert "password" not in str(error.value)


def test_close_drains_admitted_work_and_rejects_new_work(database_settings):
    async def scenario():
        database = await open_database(database_settings)
        entered, release = asyncio.Event(), asyncio.Event()

        async def work():
            async with database.transaction(write=False) as connection:
                await connection.execute(text("SELECT 1"))
                entered.set()
                await release.wait()

        writer = asyncio.create_task(work())
        await asyncio.wait_for(entered.wait(), timeout=5)
        close = asyncio.create_task(database.aclose())
        await asyncio.sleep(0)
        try:
            assert not close.done()
            with pytest.raises(DatabaseError) as error:
                await database.healthcheck()
            assert error.value.kind == "closed"
        finally:
            release.set()
            await writer
            await close
        assert database.engine.pool.checkedout() == 0

    asyncio.run(scenario())
