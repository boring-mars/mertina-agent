"""Reject invalid domain commands before they can reach a database connection."""

import asyncio
from dataclasses import replace

import pytest

from mertina_agent.exceptions import StorageError
from mertina_agent.state.models import TurnCommit
from mertina_agent.state.schema import Schema
from mertina_agent.state.serialization import encode_message
from mertina_agent.state.session_db import SessionDB


class UnavailableDatabase:
    def transaction(self, *, write=True):
        pytest.fail(f"Invalid input reached a database transaction (write={write})")


@pytest.mark.parametrize("prefix", ["", "../table", "name; DROP", "a" * 41, "Upper_", "1prefix"])
def test_schema_namespace_rejects_arbitrary_identifiers(prefix):
    with pytest.raises(StorageError, match="Invalid storage namespace"):
        Schema(prefix)


@pytest.mark.parametrize("title", [None, 123, "x" * 201, "null\x00byte", "surrogate\ud800"])
def test_invalid_title_does_not_open_a_database_transaction(title):
    store = SessionDB(UnavailableDatabase())
    with pytest.raises(StorageError, match="Session title"):
        asyncio.run(store.create_session(title))


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5, "10"])
def test_invalid_session_page_limit_is_rejected(limit):
    store = SessionDB(UnavailableDatabase())
    with pytest.raises(StorageError, match="page limit"):
        asyncio.run(store.list_sessions(limit=limit))


@pytest.mark.parametrize("version", [-1, True, 1.5, 2**31 - 1])
def test_invalid_rename_version_is_rejected(version):
    store = SessionDB(UnavailableDatabase())
    with pytest.raises(StorageError, match="counter"):
        asyncio.run(store.rename_session("1" * 32, "Valid", expected_version=version))


@pytest.mark.parametrize(
    "fields",
    [
        {"after": -1},
        {"after": True},
        {"after": 2**31},
        {"through": -1},
        {"through": 1.5},
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
    ],
)
def test_invalid_message_page_is_rejected(fields):
    store = SessionDB(UnavailableDatabase())
    with pytest.raises(StorageError):
        asyncio.run(store.read_messages("1" * 32, **fields))


@pytest.mark.parametrize(
    "fields",
    [
        {"session_id": "invalid"},
        {"operation_id": "A" * 32},
        {"operation_id": None},
        {"expected_count": -1},
        {"expected_revision": True},
        {"expected_revision": 2**31 - 1},
        {"expected_count": 2**31 - 1},
        {"timestamp_us": -1},
        {"timestamp_us": 2**63},
        {"payloads": ()},
        {"payloads": ("[]",)},
        {"payloads": (None,)},
        {"status": "unknown"},
    ],
)
def test_invalid_turn_command_is_rejected_before_io(fields):
    store = SessionDB(UnavailableDatabase())
    command = TurnCommit(
        "1" * 32,
        "2" * 32,
        0,
        0,
        (encode_message({"role": "user", "content": "Valid"}),),
        "completed",
        100,
    )
    with pytest.raises(StorageError):
        asyncio.run(store.commit_turn(replace(command, **fields)))
