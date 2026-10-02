"""The worker-thread request: the caller polls for interrupts while the worker owns its client."""

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from mertina.agent.chat_completion_helpers import _RequestClientRegistry, interruptible_api_call


class _Agent:
    """Hands out one recorded client per request and logs what happens to it."""

    def __init__(self, create: Any) -> None:
        self._interrupt_requested = False
        self.events: list[tuple[str, str, int]] = []
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

    def _create_request_openai_client(self, *, reason: str) -> Any:
        return self.client

    def _close_request_openai_client(self, client: Any, *, reason: str) -> None:
        self.events.append(("close", reason, threading.get_ident()))

    def _abort_request_openai_client(self, client: Any, *, reason: str) -> None:
        self.events.append(("abort", reason, threading.get_ident()))


def test_the_response_comes_back_and_the_worker_keeps_its_client_warm() -> None:
    agent = _Agent(lambda **kwargs: {"echo": kwargs["model"]})

    assert interruptible_api_call(agent, {"model": "m"}) == {"echo": "m"}
    assert [e[:2] for e in agent.events] == [("close", "request_complete")]
    assert agent.events[0][2] != threading.get_ident()


def test_a_provider_error_is_raised_in_the_caller() -> None:
    def fail(**kwargs: Any) -> Any:
        raise ConnectionError("reset by peer")

    agent = _Agent(fail)

    with pytest.raises(ConnectionError, match="reset by peer"):
        interruptible_api_call(agent, {"model": "m"})
    assert [e[:2] for e in agent.events] == [("close", "request_error_cleanup")]


def test_a_stop_raises_at_once_and_the_worker_closes_its_own_client() -> None:
    entered, aborted = threading.Event(), threading.Event()

    def hang(**kwargs: Any) -> Any:
        entered.set()
        aborted.wait(timeout=5)
        raise ConnectionError("socket shut down")

    agent = _Agent(hang)
    original_abort = agent._abort_request_openai_client

    def abort(client: Any, *, reason: str) -> None:
        original_abort(client, reason=reason)
        aborted.set()

    agent._abort_request_openai_client = abort  # type: ignore[method-assign]  # record and release

    def stop() -> None:
        entered.wait(timeout=5)
        agent._interrupt_requested = True

    threading.Thread(target=stop).start()

    started = time.monotonic()
    with pytest.raises(InterruptedError):
        interruptible_api_call(agent, {"model": "m"})

    assert time.monotonic() - started < 2
    abort_event = agent.events[0]
    assert abort_event[:2] == ("abort", "interrupt_abort")
    assert abort_event[2] == threading.get_ident()  # the poll thread only aborts
    deadline = time.monotonic() + 5
    while len(agent.events) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    close_event = agent.events[1]
    assert close_event[:2] == ("close", "request_error_cleanup")
    assert close_event[2] != threading.get_ident()  # the worker closes its own client


def test_the_registry_closes_on_the_owner_thread_and_aborts_from_any_other() -> None:
    agent = _Agent(lambda **kwargs: None)
    registry = _RequestClientRegistry(agent)
    client = object()
    worker = threading.Thread(target=lambda: registry.set_client(client))
    worker.start()
    worker.join()

    registry.close_once("interrupt_abort")
    assert registry.client is client  # still the worker's to close
    closer = threading.Thread(target=lambda: registry.close_once("request_error_cleanup"))
    closer.start()
    closer.join()

    assert [e[:2] for e in agent.events] == [
        ("abort", "interrupt_abort"),
        ("abort", "request_error_cleanup"),
    ]
    registry.owner_tid = threading.get_ident()
    registry.close_once("request_complete")
    assert registry.client is None
    assert agent.events[-1][:2] == ("close", "request_complete")
    registry.close_once("request_complete")  # nothing left to close
    assert len(agent.events) == 3
