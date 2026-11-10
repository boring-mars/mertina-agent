import asyncio
from dataclasses import FrozenInstanceError

import pytest

from mertina_agent.exceptions import (
    ConfigurationError,
    EventCursorError,
    EventPayloadError,
    EventSubscriptionError,
)
from mertina_agent.gateway.run_events import MAX_EVENT_BYTES, EventBuffer


def make_buffer(*, capacity=3, subscribers=2):
    return EventBuffer(
        "run", capacity=capacity, subscriber_capacity=capacity, max_subscribers=subscribers
    )


def test_replay_and_live_attachment_have_no_gap_and_broadcast_to_all():
    buffer = make_buffer()
    first = buffer.publish("text_delta", {"text": "first"})
    a = buffer.subscribe(0)
    b = buffer.subscribe(first.id)
    second = buffer.publish("text_delta", {"text": "second"})
    assert a.queue.get_nowait() == first
    assert a.queue.get_nowait() == second
    assert b.queue.get_nowait() == second
    assert a.queue.empty()
    assert b.queue.empty()
    assert buffer.latest_id == 2
    a.close()
    third = buffer.publish("text_delta", {"text": "third"})
    assert b.queue.get_nowait() == third
    assert a.queue.get_nowait() is None
    assert not b.closed
    b.close()


def test_events_snapshot_nested_data_and_are_immutable():
    buffer = make_buffer()
    data = {"tool": {"name": "web_search", "arguments": ["original"]}}
    event = buffer.publish("tool_call_started", data)
    data["tool"]["arguments"].append("changed")
    exposed = event.data
    exposed["tool"]["arguments"].append("also changed")
    snapshot = event.to_dict()
    snapshot["type"] = "changed"
    assert event.data == {"tool": {"name": "web_search", "arguments": ["original"]}}
    assert event.to_dict()["type"] == "tool_call_started"
    with pytest.raises(FrozenInstanceError):
        event.id = 2


def test_retained_suffix_includes_the_oldest_event_and_rejects_expired_cursor():
    buffer = make_buffer(capacity=2)
    for number in range(4):
        buffer.publish("text_delta", {"number": number})
    with pytest.raises(EventCursorError, match="resynchronization"):
        buffer.subscribe(1)
    replay = buffer.subscribe(2)
    assert [replay.queue.get_nowait().id, replay.queue.get_nowait().id] == [3, 4]
    replay.close()
    current = buffer.subscribe(4)
    assert current.queue.empty()
    current.close()


@pytest.mark.parametrize("cursor", [-1, 1, True, False, 0.0, "0", None])
def test_invalid_and_future_cursors_are_explicitly_rejected(cursor):
    with pytest.raises(EventCursorError):
        make_buffer().subscribe(cursor)


def test_slow_subscriber_is_disconnected_without_affecting_an_active_reader():
    buffer = make_buffer(capacity=2)
    slow = buffer.subscribe(0)
    fast = buffer.subscribe(0)
    for number in range(3):
        event = buffer.publish("text_delta", {"number": number})
        assert fast.queue.get_nowait() == event
    assert slow.closed
    assert slow.resync_required
    assert slow.queue.qsize() == 1
    assert slow.queue.get_nowait() is None
    assert not fast.closed
    assert not fast.resync_required
    slow.close()
    assert slow.resync_required
    # An evicted subscriber frees its admission slot immediately.
    replacement = buffer.subscribe(buffer.latest_id)
    assert replacement.queue.empty()
    replacement.close()
    fast.close()


def test_finish_preserves_a_full_queue_and_supports_terminal_reconnect():
    buffer = make_buffer(capacity=2)
    subscriber = buffer.subscribe(0)
    first = buffer.publish("text_delta", {"text": "answer"})
    terminal = buffer.publish("run_completed", {"durable": True})
    buffer.finish()
    buffer.finish()
    assert subscriber.queue.maxsize == 3
    assert [subscriber.queue.get_nowait() for _ in range(3)] == [first, terminal, None]
    subscriber.close()
    replay = buffer.subscribe(0)
    assert [replay.queue.get_nowait() for _ in range(3)] == [first, terminal, None]
    replay.close()
    current = buffer.subscribe(2)
    assert current.queue.get_nowait() is None
    with pytest.raises(EventPayloadError):
        buffer.publish("text_delta", {"text": "too late"})
    current.close()


def test_force_close_discards_queued_events_and_wakes_finished_subscribers():
    buffer = make_buffer()
    subscriber = buffer.subscribe(0)
    buffer.publish("text_delta", {"text": "temporary"})
    buffer.finish()
    buffer.close()
    buffer.close()
    assert buffer.closed
    assert subscriber.closed
    assert subscriber.queue.get_nowait() is None
    assert subscriber.queue.empty()
    with pytest.raises(EventSubscriptionError):
        buffer.subscribe(0)
    with pytest.raises(EventPayloadError):
        buffer.publish("text_delta", {})


def test_waiting_subscriber_wakes_when_closed():
    async def scenario():
        buffer = make_buffer()
        subscriber = buffer.subscribe(0)
        waiting = asyncio.create_task(subscriber.queue.get())
        await asyncio.sleep(0)
        subscriber.close()
        assert await asyncio.wait_for(waiting, 1) is None
        assert not buffer.closed
        buffer.publish("text_delta", {"text": "run continues"})

    asyncio.run(scenario())


def test_subscriber_limit_applies_to_finished_streams_until_they_close():
    buffer = make_buffer(subscribers=1)
    subscriber = buffer.subscribe(0)
    buffer.finish()
    with pytest.raises(EventSubscriptionError):
        buffer.subscribe(0)
    subscriber.close()
    replacement = buffer.subscribe(0)
    assert replacement.queue.get_nowait() is None
    replacement.close()


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("bad\nevent", {}),
        ("", {}),
        ("x" * 65, {}),
        (None, {}),
        ("valid", []),
        ("valid", {"value": float("inf")}),
        ("valid", {"value": float("nan")}),
        ("valid", {"value": object()}),
        ("valid", {"value": "a" * MAX_EVENT_BYTES}),
        ("valid", {"value": "中" * (MAX_EVENT_BYTES // 3)}),
    ],
)
def test_invalid_or_oversized_events_do_not_consume_a_sequence(kind, data):
    buffer = make_buffer()
    with pytest.raises(EventPayloadError):
        buffer.publish(kind, data)
    assert buffer.latest_id == 0
    assert buffer.publish("valid", {}).id == 1


def test_cyclic_payload_is_rejected_without_leaking_its_content():
    data = {"secret": "private-value"}
    data["cycle"] = data
    with pytest.raises(EventPayloadError, match="finite JSON") as caught:
        make_buffer().publish("valid", data)
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize(
    ("capacity", "subscriber_capacity", "max_subscribers"),
    [(0, 1, 1), (1, 0, 1), (2, 1, 1), (1, 1, 0), (True, 1, 1), (1, 1.0, 1)],
)
def test_configuration_bounds_are_validated(capacity, subscriber_capacity, max_subscribers):
    with pytest.raises(ConfigurationError):
        EventBuffer(
            "run",
            capacity=capacity,
            subscriber_capacity=subscriber_capacity,
            max_subscribers=max_subscribers,
        )
