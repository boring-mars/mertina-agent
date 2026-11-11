"""MySQL asyncmy engine using utf8mb4 and explicit transaction isolation."""

import ssl
from collections.abc import Mapping
from typing import TypedDict

from sqlalchemy import text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from mertina_agent.config import Settings
from mertina_agent.exceptions import ConfigurationError


class TableOptions(TypedDict):
    """Required dialect options for application-owned transactional tables."""

    mysql_engine: str
    mysql_charset: str
    mysql_collate: str


def table_options() -> TableOptions:
    """Use transactional storage and case-sensitive, lossless Unicode text."""
    return {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_bin",
    }


def valid_table_options(options: Mapping[str, object]) -> bool:
    """Check reflected table options, including MySQL's DEFAULT CHARSET spelling."""
    return (
        str(options.get("mysql_engine", "")).lower() == "innodb"
        and options.get("mysql_default charset", options.get("mysql_charset")) == "utf8mb4"
        and options.get("mysql_collate") == "utf8mb4_bin"
    )


def payload_type() -> LONGTEXT:
    """Store bounded message payloads beyond MySQL TEXT's 64 KiB ceiling."""
    return LONGTEXT()


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
