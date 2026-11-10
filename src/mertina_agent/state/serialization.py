"""Versioned, lossless message encoding using the existing transport contract."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import cast

from mertina_agent.agent.transports.chat_completions import ChatCompletionsTransport
from mertina_agent.agent.transports.types import ChatMessage
from mertina_agent.agent.turn_result import ConversationResult
from mertina_agent.exceptions import ModelInputError, StorageError
from mertina_agent.state.models import HistorySnapshot, TurnCommit, TurnStatus

MAX_MESSAGE_BYTES = 1024 * 1024
MAX_TURN_BYTES = 8 * MAX_MESSAGE_BYTES


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            message = "Stored message contains duplicate object keys"
            raise StorageError(message)
        result[key] = value
    return result


def encode_message(message: ChatMessage) -> str:
    """Preserve absent/null fields and opaque tool argument strings exactly."""
    try:
        ChatCompletionsTransport().convert_messages([message])
        encoded = json.dumps(
            {"version": 1, "message": message},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (ModelInputError, TypeError, ValueError) as exc:
        reason = "Message cannot be encoded for storage"
        raise StorageError(reason) from exc
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
        reason = "Message exceeds the storage size limit"
        raise StorageError(reason)
    return encoded


def decode_message(payload: str) -> ChatMessage:
    """Reject corrupt or unknown versions instead of manufacturing empty history."""
    try:
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_MESSAGE_BYTES:
            reason = "Stored message exceeds the storage size limit"
            raise StorageError(reason)
        envelope = json.loads(payload, object_pairs_hook=_unique_object)
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"version", "message"}
            or type(envelope["version"]) is not int
            or envelope["version"] != 1
            or not isinstance(envelope["message"], dict)
        ):
            reason = "Unsupported stored message envelope"
            raise StorageError(reason)
        message = cast(ChatMessage, envelope["message"])
        encode_message(message)
        return message
    except (ValueError, TypeError, RecursionError) as exc:
        reason = "Stored message is corrupt"
        raise StorageError(reason) from exc


def turn_status(result: ConversationResult) -> TurnStatus:
    """Classify a returned turn the same way for every caller that commits it."""
    if result["interrupted"]:
        return "stopped"
    if result["failed"]:
        return "failed"
    if result["completed"]:
        return "completed"
    return "partial"


def build_commit(
    snapshot: HistorySnapshot,
    messages: Sequence[ChatMessage],
    *,
    operation_id: str,
    status: TurnStatus,
    timestamp_us: int,
) -> TurnCommit:
    """Verify the old prefix and freeze only the new suffix for one atomic commit."""
    if snapshot.session.message_count != len(snapshot.messages):
        reason = "History snapshot message count is inconsistent"
        raise StorageError(reason)
    encoded = tuple(encode_message(message) for message in messages)
    prefix = tuple(encode_message(message) for message in snapshot.messages)
    if encoded[: len(prefix)] != prefix or len(encoded) <= len(prefix):
        reason = "Turn result must preserve history and append new messages"
        raise StorageError(reason)
    suffix = encoded[len(prefix) :]
    if sum(len(value.encode("utf-8")) for value in suffix) > MAX_TURN_BYTES:
        reason = "Turn exceeds the storage size limit"
        raise StorageError(reason)
    return TurnCommit(
        snapshot.session.id,
        operation_id,
        snapshot.session.history_revision,
        snapshot.session.message_count,
        suffix,
        status,
        timestamp_us,
    )


def fingerprint(command: TurnCommit) -> str:
    """Hash every field that can affect an idempotent write."""
    encoded = json.dumps(asdict(command), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
