"""MySQL asyncmy engine using utf8mb4 and explicit transaction isolation."""

import ssl

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from mertina_agent.config import Settings
from mertina_agent.exceptions import ConfigurationError


def create_engine(url: URL, settings: Settings) -> AsyncEngine:
    """Build the MySQL pool with Unicode and bounded network operations."""
    return create_async_engine(
        url,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_connect_timeout_s,
        isolation_level="READ COMMITTED",
        connect_args={
            "charset": "utf8mb4",
            "connect_timeout": settings.database_connect_timeout_s,
            "read_timeout": settings.database_timeout_s,
            "ssl": ssl.create_default_context() if settings.database_ssl else None,
        },
    )


async def check_connection(connection: AsyncConnection) -> None:
    """Require transactional tables and lossless Unicode on the selected server."""
    result = await connection.execute(
        text("SELECT @@default_storage_engine, @@character_set_connection")
    )
    engine, charset = result.one()
    if str(engine).lower() != "innodb" or charset != "utf8mb4":
        message = "MySQL requires InnoDB and utf8mb4"
        raise ConfigurationError(message)
