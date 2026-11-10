"""Lossless storage and immutable turn commands use the Agent's real contract."""

import json
from dataclasses import replace

import pytest

from mertina_agent.exceptions import StorageError
from mertina_agent.state.models import HistorySnapshot, SessionRecord
from mertina_agent.state.serialization import (
    MAX_MESSAGE_BYTES,
    MAX_TURN_BYTES,
    build_commit,
    decode_message,
    encode_message,
    fingerprint,
)


@pytest.mark.parametrize(
    "message",
    [
        {"role": "system", "content": "System\n中文🐬"},
        {"role": "developer", "content": "Rules"},
        {"role": "user", "content": "Question\x00"},
        {"role": "assistant", "content": "Answer"},
        {"role": "assistant", "content": "", "tool_calls": None, "refusal": None},
        {"role": "assistant", "refusal": "Declined"},
        {"role": "assistant", "content": None, "refusal": "Declined", "tool_calls": []},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{ "value": "\\u4e2d" }\n'},
                }
            ],
        },
        {"role": "tool", "content": '{"result":null}', "tool_call_id": "call-1"},
    ],
)
def test_message_round_trip_preserves_fields_nulls_and_opaque_arguments(message):
    payload = encode_message(message)
    assert decode_message(payload) == message
    assert encode_message(decode_message(payload)) == payload


@pytest.mark.parametrize(
    "message",
    [
        {"role": "invalid", "content": "text"},
        {"role": "user", "content": None},
        {"role": "user", "content": "text", "unexpected": "no"},
        {"role": "assistant", "content": None},
        {"role": "tool", "content": "result"},
        {"role": "tool", "content": "result", "tool_call_id": ""},
    ],
)
def test_invalid_message_is_rejected(message):
    with pytest.raises(StorageError, match="cannot be encoded"):
        encode_message(message)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        "null",
        '{"version":1}',
        '{"version":2,"message":{"role":"user","content":"text"}}',
        '{"version":true,"message":{"role":"user","content":"text"}}',
        '{"version":1,"message":[],"unknown":1}',
        '{"version":1,"message":{"role":"user","content":null}}',
        '{"version":1,"version":2,"message":{"role":"user","content":"text"}}',
        '{"version":1,"message":{"role":"user","content":"a","content":"b"}}',
        "[" * 2000 + "]" * 2000,
    ],
)
def test_corrupt_stored_payload_fails_closed(payload):
    with pytest.raises(StorageError):
        decode_message(payload)


def test_message_size_limit_applies_before_parsing_and_after_encoding():
    with pytest.raises(StorageError, match="size limit"):
        decode_message(" " * (MAX_MESSAGE_BYTES + 1))
    with pytest.raises(StorageError, match="size limit"):
        encode_message({"role": "user", "content": "a" * MAX_MESSAGE_BYTES})


def _snapshot(messages=()):
    return HistorySnapshot(
        SessionRecord("1" * 32, "title", 100, 100, 1, 0, len(messages)),
        tuple(messages),
    )


def _commit(snapshot, messages):
    return build_commit(
        snapshot, messages, operation_id="2" * 32, status="completed", timestamp_us=200
    )


def test_commit_freezes_only_suffix_and_stable_fingerprint():
    old = {"role": "user", "content": "before"}
    answer = {"role": "assistant", "content": "after"}
    command = _commit(_snapshot([old]), [old, answer])
    assert command.payloads == (encode_message(answer),)
    answer["content"] = "mutated later"
    assert decode_message(command.payloads[0])["content"] == "after"
    assert fingerprint(command) == fingerprint(replace(command))
    for changed in (
        replace(command, status="stopped"),
        replace(command, timestamp_us=201),
        replace(command, operation_id="3" * 32),
        replace(command, session_id="4" * 32),
        replace(command, expected_revision=2),
        replace(command, expected_count=2),
        replace(command, payloads=(encode_message(answer),)),
    ):
        assert fingerprint(command) != fingerprint(changed)


@pytest.mark.parametrize(
    "result",
    [
        [],
        [{"role": "user", "content": "before"}],
        [{"role": "user", "content": "changed"}, {"role": "assistant", "content": "after"}],
    ],
)
def test_commit_requires_an_unchanged_prefix_and_new_suffix(result):
    with pytest.raises(StorageError, match="preserve history"):
        _commit(_snapshot([{"role": "user", "content": "before"}]), result)


def test_inconsistent_snapshot_is_rejected():
    snapshot = _snapshot()
    snapshot = replace(snapshot, session=replace(snapshot.session, message_count=1))
    with pytest.raises(StorageError, match="inconsistent"):
        _commit(snapshot, [{"role": "user", "content": "new"}])


def test_total_turn_size_is_bounded_even_when_each_message_is_valid():
    message = {"role": "user", "content": "a" * (MAX_MESSAGE_BYTES // 2)}
    count = MAX_TURN_BYTES // len(encode_message(message)) + 1
    with pytest.raises(StorageError, match="Turn exceeds"):
        _commit(_snapshot(), [message] * count)


def test_object_key_order_does_not_change_canonical_payload():
    message = {"role": "assistant", "content": "a", "refusal": None}
    reordered = dict(reversed(list(message.items())))
    assert json.dumps(message) != json.dumps(reordered)
    assert encode_message(message) == encode_message(reordered)
