"""Local SQLite with explicit transactions, foreign keys and durable WAL writes."""

from typing import Protocol, cast

from sqlalchemy import event
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

from mertina_agent.config import Settings


class _Cursor(Protocol):
    def execute(self, statement: str) -> object: ...
    def close(self) -> None: ...


class _Connection(Protocol):
    isolation_level: str | None

    def cursor(self) -> _Cursor: ...


def create_engine(url: URL, settings: Settings) -> AsyncEngine:
    """Build an async engine; all SQLite details stay in this package."""
    engine = create_async_engine(url, hide_parameters=True, pool_pre_ping=True)

    @event.listens_for(engine.sync_engine, "connect")
    def configure(connection: object, _record: ConnectionPoolEntry) -> None:
        adapted = cast(_Connection, connection)
        # Disable sqlite3's legacy implicit transaction rules, including for DDL.
        adapted.isolation_level = None
        cursor = adapted.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={settings.database_sqlite_busy_timeout_ms}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def begin(connection: Connection) -> None:
        # Reserve the writer before a read/modify/write operation can see a stale snapshot.
        statement = (
            "BEGIN IMMEDIATE" if connection.get_execution_options().get("write") else "BEGIN"
        )
        connection.exec_driver_sql(statement)

    return engine
