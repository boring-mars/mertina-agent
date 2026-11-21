"""Own a database pool and bounded transactions for relational repositories."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mertina_agent.config import Settings
from mertina_agent.database.config import DRIVERS
from mertina_agent.database.mysql import adapter as mysql
from mertina_agent.database.postgresql import adapter as postgresql
from mertina_agent.database.sqlite import adapter as sqlite
from mertina_agent.exceptions import ConfigurationError, DatabaseError


class Database:
    """Own an engine; domain repositories use its transaction boundary.

    Instances belong to one event loop. Close after stopping new work; close
    waits for already admitted bounded operations and then disposes the pool.
    SQL/driver objects never cross from repositories into the Agent or HTTP API.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        check_connection: Callable[[AsyncConnection], Awaitable[None]] | None = None,
    ) -> None:
        """Use :func:`open_database` for validated and health-checked construction."""
        self.engine = engine
        self.backend = engine.dialect.name
        self._timeout_s = settings.database_timeout_s
        self._check_connection = check_connection
        self._closed = False
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        # In-memory SQLite shares one physical connection. Serial admission also
        # avoids unnecessary writer contention for file-backed local operation.
        self._sqlite_lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self, *, write: bool = True) -> AsyncIterator[AsyncConnection]:
        """Commit on success and roll back on failure, timeout or cancellation.

        No automatic write retry occurs. A commit failure is reported as unknown
        because a disconnected server may already have made the write durable.
        """
        if self._closed:
            raise DatabaseError(kind="closed")
        self._active += 1
        self._idle.clear()
        committing = False
        locked = False
        try:
            async with asyncio.timeout(self._timeout_s):
                if self.backend == "sqlite":
                    await self._sqlite_lock.acquire()
                    locked = True
                async with self.engine.connect() as connection:
                    await connection.execution_options(write=write)
                    async with connection.begin():
                        yield connection
                        committing = True
        except (TimeoutError, PoolTimeoutError) as exc:
            raise DatabaseError("unknown" if committing else "timeout") from exc
        except IntegrityError as exc:
            raise DatabaseError(kind="constraint") from exc
        except (OperationalError, OSError) as exc:
            raise DatabaseError("unknown" if committing else "connection") from exc
        except SQLAlchemyError as exc:
            raise DatabaseError("unknown" if committing else "query") from exc
        finally:
            if locked:
                self._sqlite_lock.release()
            self._active -= 1
            if self._active == 0:
                self._idle.set()

    async def healthcheck(self) -> None:
        """Verify a real round trip and the backend's required capabilities."""
        async with self.transaction(write=False) as connection:
            await connection.execute(text("SELECT 1"))
            if self._check_connection is not None:
                await self._check_connection(connection)

    async def aclose(self) -> None:
        """Reject new work, drain admitted operations and release connections."""
        self._closed = True
        await self._idle.wait()
        await self.engine.dispose()

    async def __aenter__(self) -> Self:
        """Enter the lifetime of this database."""
        if self._closed:
            raise DatabaseError(kind="closed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Dispose owned connections on context exit."""
        await self.aclose()


async def open_database(settings: Settings) -> Database:
    """Open exactly the configured backend; never fall back to another database."""
    try:
        url = make_url(settings.database_url.get_secret_value())
        url = url.set(drivername=DRIVERS[url.drivername])
        backend = url.get_backend_name()
        if backend == "sqlite" and url.database != ":memory:":
            path = Path(url.database or "").expanduser().resolve()
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            url = url.set(database=str(path))
        builder = {"sqlite": sqlite, "postgresql": postgresql, "mysql": mysql}[backend]
        engine = builder.create_engine(url, settings)
    except (ValueError, KeyError, ImportError, SQLAlchemyError) as exc:
        message = "Database configuration or driver is unavailable"
        raise ConfigurationError(message) from exc
    except OSError as exc:
        raise DatabaseError(kind="connection") from exc
    database = Database(engine, settings, mysql.check_connection if backend == "mysql" else None)
    try:
        async with asyncio.timeout(settings.database_connect_timeout_s):
            await database.healthcheck()
    except TimeoutError as exc:
        await database.aclose()
        raise DatabaseError(kind="timeout") from exc
    except DatabaseError as exc:
        await database.aclose()
        if exc.kind in {"query", "constraint"}:
            raise DatabaseError(kind="connection") from exc
        raise
    except BaseException:
        # Lifecycle boundary: a failed or cancelled open must not leave a pool.
        await database.aclose()
        raise
    return database
