"""Portable minimal session schema adapted from Hermes's SessionDB responsibilities.

Source: hermes_state_common.py SCHEMA_SQL and hermes_state_schema.py at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-gateway-sessions.md.
SQL is intentionally rebuilt with Core for three backends and atomic turn receipts.
"""

import re

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    inspect,
    select,
)
from sqlalchemy.engine import Connection

from mertina_agent.database.mysql.adapter import payload_type, table_options, valid_table_options
from mertina_agent.database.runtime import Database
from mertina_agent.exceptions import StorageError


def _compatible_type(actual: object, expected: object) -> bool:
    if not isinstance(actual, type(expected)):
        return False
    if isinstance(expected, String):
        return isinstance(actual, String) and actual.length == expected.length
    return True


def _validate_structure(connection: Connection, metadata: MetaData) -> None:
    """Reject stamped tables that no longer enforce the storage invariants."""
    inspector = inspect(connection)
    for table in metadata.sorted_tables:
        columns = inspector.get_columns(table.name)
        actual = {column["name"]: column for column in columns}
        expected = {column.name: column for column in table.columns}
        valid = set(actual) == set(expected)
        if valid:
            valid = all(
                actual[name]["nullable"] == column.nullable
                and _compatible_type(
                    actual[name]["type"],
                    column.type.dialect_impl(connection.dialect)
                    if connection.dialect.name == "mysql"
                    else column.type,
                )
                for name, column in expected.items()
            )
        primary_key = inspector.get_pk_constraint(table.name)["constrained_columns"]
        valid = valid and primary_key == [column.name for column in table.primary_key]
        foreign_keys = inspector.get_foreign_keys(table.name)
        expected_keys = list(table.foreign_key_constraints)
        valid = valid and len(foreign_keys) == len(expected_keys)
        for expected_key in expected_keys:
            valid = valid and any(
                key["constrained_columns"] == [column.name for column in expected_key.columns]
                and key["referred_table"] == expected_key.referred_table.name
                and key["referred_columns"] == [item.column.name for item in expected_key.elements]
                and key.get("options", {}).get("ondelete", "").upper() == "CASCADE"
                for key in foreign_keys
            )
        if connection.dialect.name == "mysql":
            options = inspector.get_table_options(table.name)
            valid = valid and valid_table_options(options)
            if "payload" in actual:
                valid = valid and isinstance(actual["payload"]["type"], type(payload_type()))
        if not valid:
            message = "Storage schema structure differs from version 1"
            raise StorageError(message)


class Schema:
    """Two business tables and two technical tables in one explicit namespace."""

    def __init__(self, prefix: str = "mertina_") -> None:
        """Allow isolated test namespaces, never interpolating arbitrary identifiers."""
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", prefix):
            message = "Invalid storage namespace"
            raise StorageError(message)
        self.metadata = MetaData()
        options = table_options()
        self.version = Table(
            prefix + "schema_version",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=False),
            Column("version", Integer, nullable=False),
            **options,
        )
        self.sessions = Table(
            prefix + "sessions",
            self.metadata,
            Column("id", String(32), primary_key=True),
            Column("title", String(200), nullable=False),
            Column("created_at_us", BigInteger, nullable=False, index=True),
            Column("updated_at_us", BigInteger, nullable=False),
            Column("history_revision", Integer, nullable=False),
            Column("metadata_version", Integer, nullable=False),
            Column("message_count", Integer, nullable=False),
            **options,
        )
        self.messages = Table(
            prefix + "messages",
            self.metadata,
            Column(
                "session_id",
                String(32),
                ForeignKey(self.sessions.c.id, ondelete="CASCADE"),
                primary_key=True,
            ),
            Column("seq", Integer, primary_key=True, autoincrement=False),
            Column("payload", Text().with_variant(payload_type(), "mysql"), nullable=False),
            **options,
        )
        self.commits = Table(
            prefix + "commits",
            self.metadata,
            Column(
                "session_id",
                String(32),
                ForeignKey(self.sessions.c.id, ondelete="CASCADE"),
                primary_key=True,
            ),
            Column("operation_id", String(32), primary_key=True),
            Column("fingerprint", String(64), nullable=False),
            Column("revision", Integer, nullable=False),
            Column("first_seq", Integer, nullable=False),
            Column("last_seq", Integer, nullable=False),
            Column("status", String(16), nullable=False),
            **options,
        )

    async def initialize(self, database: Database, *, bootstrap: bool = False) -> None:
        """Check v1 or explicitly create an empty namespace; never auto-upgrade.

        MySQL DDL commits implicitly: a partially created namespace is rejected
        on the next startup rather than silently stamped as a complete migration.
        Run bootstrap with a single initializer, before starting the Gateway.
        """
        async with database.transaction(write=bootstrap) as connection:
            names = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
            expected = set(self.metadata.tables)
            present = names & expected
            if not present and bootstrap:
                await connection.run_sync(self.metadata.create_all)
                await connection.execute(self.version.insert().values(id=1, version=1))
            elif present != expected:
                message = (
                    "Storage schema missing or incomplete; initialize an empty namespace explicitly"
                )
                raise StorageError(message)
            versions = (await connection.execute(select(self.version))).all()
            if versions != [(1, 1)]:
                message = "Unsupported storage schema version; an explicit migration is required"
                raise StorageError(message)
            await connection.run_sync(_validate_structure, self.metadata)
            # Fail during startup for missing columns, not halfway through a run.
            for table in self.metadata.sorted_tables:
                await connection.execute(select(table).limit(0))
