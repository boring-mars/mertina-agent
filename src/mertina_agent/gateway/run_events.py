"""Bounded, process-local event replay and broadcast for HTTP run observers.

Unlike Hermes' destructive run transport queue, every subscriber sees its own
ordered stream. Replay and live attachment are synchronous on one event loop;
disconnecting a subscriber never changes the run's lifecycle.
"""

import asyncio
import json
import re
from collections import deque
from dataclasses import dataclass
from typing import cast

from mertina_agent.exceptions import (
    ConfigurationError,
    EventCursorError,
    EventPayloadError,
    EventSubscriptionError,
)

MAX_EVENT_BYTES = 65_536
_EVENT_TYPE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}\Z")


def _serialize(value: dict[str, object]) -> str:
    """Encode an independent JSON snapshot with a bounded retained size."""
    chunks: list[str] = []
    length = 0
    encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    try:
        for chunk in encoder.iterencode(value):
            length += len(chunk)
            if length > MAX_EVENT_BYTES:
                message = "Event payload exceeds the size limit"
                raise EventPayloadError(message)
            chunks.append(chunk)
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        message = "Event payload must contain finite JSON values"
        raise EventPayloadError(message) from exc
    return "".join(chunks)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """One immutable event with a sequence number scoped to its run.

    The internal JSON snapshot protects retained replay from mutation by the
    publisher or any subscriber. ``data`` and ``to_dict`` return fresh values.
    """

    id: int
    run_id: str
    type: str
    _json: str

    @property
    def data(self) -> dict[str, object]:
        """Return an independent copy of the event's JSON object."""
        return cast(dict[str, object], json.loads(self._json)["data"])

    def to_dict(self) -> dict[str, object]:
        """Return the complete JSON-safe envelope for HTTP serialization."""
        return cast(dict[str, object], json.loads(self._json))


class Subscription:
    """A bounded independent stream; None ends the queue.

    Inspect ``resync_required`` after EOF: overflow requires obtaining a run
    snapshot before reconnecting. Always call ``close`` in the HTTP handler's
    ``finally`` block, including after normal EOF.
    """

    def __init__(self, buffer: "EventBuffer", capacity: int) -> None:
        """Create a queue with one extra slot reserved for EOF notification."""
        self.queue: asyncio.Queue[EventEnvelope | None] = asyncio.Queue(capacity + 1)
        self.resync_required = False
        self._buffer = buffer
        self._closed = False

    @property
    def closed(self) -> bool:
        """Report whether the subscriber has been detached from the buffer."""
        return self._closed

    def close(self) -> None:
        """Detach immediately without cancelling or stopping the run."""
        self._buffer._disconnect(self, resync=False)


class EventBuffer:
    """Keep bounded replay and broadcast without awaiting slow consumers.

    All operations must execute on one event loop. A subscriber's configured
    event capacity must cover the replay capacity, so every retained suffix
    can attach atomically. One additional queue slot is reserved for EOF.
    ``finish`` retains replay; ``close`` discards it during registry eviction.
    """

    def __init__(
        self,
        run_id: str,
        *,
        capacity: int,
        subscriber_capacity: int,
        max_subscribers: int,
    ) -> None:
        """Set bounds for retained events and independent subscriber queues."""
        bounds = (capacity, subscriber_capacity, max_subscribers)
        if (
            any(type(value) is not int or value < 1 for value in bounds)
            or subscriber_capacity < capacity
            or not isinstance(run_id, str)
            or not run_id
            or len(run_id) > 128
        ):
            message = "Invalid event buffer limits or run ID"
            raise ConfigurationError(message)
        self._run_id = run_id
        self._events: deque[EventEnvelope] = deque(maxlen=capacity)
        self._subscriber_capacity = subscriber_capacity
        self._max_subscribers = max_subscribers
        self._subscribers: set[Subscription] = set()
        self._latest_id = 0
        self._finished = False
        self._closed = False

    @property
    def latest_id(self) -> int:
        """Return the last published sequence, or zero before any events."""
        return self._latest_id

    @property
    def closed(self) -> bool:
        """Report force closure, which also invalidates retained replay."""
        return self._closed

    def publish(self, event_type: str, data: dict[str, object]) -> EventEnvelope:
        """Publish to all attached observers; detach overflowing consumers.

        Raises:
            EventPayloadError: If closed, finished or the event is invalid.
        """
        if (
            self._closed
            or self._finished
            or not isinstance(event_type, str)
            or _EVENT_TYPE.fullmatch(event_type) is None
            or not isinstance(data, dict)
        ):
            message = "Event cannot be published"
            raise EventPayloadError(message)
        sequence = self._latest_id + 1
        payload = _serialize(
            {"id": sequence, "run_id": self._run_id, "type": event_type, "data": data}
        )
        event = EventEnvelope(sequence, self._run_id, event_type, payload)
        self._latest_id = sequence
        self._events.append(event)
        for subscription in tuple(self._subscribers):
            if subscription.queue.qsize() >= self._subscriber_capacity:
                self._disconnect(subscription, resync=True)
            else:
                subscription.queue.put_nowait(event)
        return event

    def subscribe(self, after: int) -> Subscription:
        """Replay events after a cursor and atomically attach to live output.

        A zero cursor means no events consumed. It expires once event 1 has
        left the retained window; clients must then obtain a fresh snapshot.

        Raises:
            EventCursorError: If the cursor is invalid or no longer retained.
            EventSubscriptionError: If closed or the subscriber limit is met.
        """
        if self._closed or len(self._subscribers) >= self._max_subscribers:
            message = "Event subscriptions are unavailable"
            raise EventSubscriptionError(message)
        oldest = self._events[0].id if self._events else self._latest_id + 1
        if type(after) is not int or after < oldest - 1 or after > self._latest_id:
            message = "Event cursor requires resynchronization"
            raise EventCursorError(message)
        subscription = Subscription(self, self._subscriber_capacity)
        for event in self._events:
            if event.id > after:
                subscription.queue.put_nowait(event)
        self._subscribers.add(subscription)
        if self._finished:
            subscription.queue.put_nowait(None)
        return subscription

    def finish(self) -> None:
        """End live output after queued events, retaining replay for reconnects."""
        if self._finished or self._closed:
            return
        self._finished = True
        for subscription in self._subscribers:
            subscription.queue.put_nowait(None)

    def close(self) -> None:
        """Discard retained output and immediately terminate all observers."""
        if self._closed:
            return
        self._closed = True
        self._finished = True
        self._events.clear()
        for subscription in tuple(self._subscribers):
            self._disconnect(subscription, resync=False)

    def _disconnect(self, subscription: Subscription, *, resync: bool) -> None:
        if subscription._closed:
            return
        subscription._closed = True
        subscription.resync_required = resync
        self._subscribers.discard(subscription)
        while not subscription.queue.empty():
            subscription.queue.get_nowait()
        subscription.queue.put_nowait(None)
