"""PostgreSQL asyncpg engine with bounded connection and statement time."""

import ssl

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mertina_agent.config import Settings


def create_engine(url: URL, settings: Settings) -> AsyncEngine:
    """Build the PostgreSQL pool without opening a connection."""
    return create_async_engine(
        url,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_connect_timeout_s,
        isolation_level="READ COMMITTED",
        connect_args={
            "timeout": settings.database_connect_timeout_s,
            "command_timeout": settings.database_timeout_s,
            "ssl": ssl.create_default_context() if settings.database_ssl else False,
        },
    )
