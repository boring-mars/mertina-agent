"""Run-scoped interrupt signal and its context binding."""

import asyncio
import threading

import pytest

from mertina_agent.agent.interrupt import (
    InterruptSignal,
    bind_interrupt_signal,
    get_interrupt_reason,
    is_interrupted,
)


def test_code_outside_a_run_is_never_interrupted():
    assert is_interrupted() is False
    assert get_interrupt_reason() is None


def test_bound_signal_reports_stop_request_and_reason():
    signal = InterruptSignal()

    with bind_interrupt_signal(signal):
        assert is_interrupted() is False
        signal.set(reason="user_stop")
        assert is_interrupted() is True
        assert get_interrupt_reason() == "user_stop"


def test_clear_withdraws_stop_request_and_reason():
    signal = InterruptSignal()
    signal.set(reason="user_stop")

    signal.clear()

    assert signal.is_set() is False
    assert signal.reason is None


def test_reason_is_hidden_once_no_stop_is_requested():
    signal = InterruptSignal()

    assert signal.reason is None


def test_binding_is_restored_after_the_block():
    signal = InterruptSignal()
    signal.set()

    with bind_interrupt_signal(signal):
        pass

    assert is_interrupted() is False


def test_binding_is_restored_when_the_block_raises():
    signal = InterruptSignal()
    signal.set()

    with pytest.raises(RuntimeError), bind_interrupt_signal(signal):
        raise RuntimeError

    assert is_interrupted() is False


def test_signal_is_visible_inside_a_worker_thread():
    signal = InterruptSignal()
    signal.set()

    async def scenario():
        with bind_interrupt_signal(signal):
            return await asyncio.to_thread(is_interrupted)

    assert asyncio.run(scenario()) is True


def test_concurrent_runs_observe_only_their_own_signal():
    stopped, running = InterruptSignal(), InterruptSignal()
    stopped.set()

    async def observe(signal):
        with bind_interrupt_signal(signal):
            await asyncio.sleep(0)
            return is_interrupted()

    async def scenario():
        return await asyncio.gather(observe(stopped), observe(running))

    assert asyncio.run(scenario()) == [True, False]


def test_wait_times_out_when_no_stop_arrives():
    assert asyncio.run(InterruptSignal().wait(0.01)) is False


def test_wait_returns_at_once_when_already_stopped():
    signal = InterruptSignal()
    signal.set()

    assert asyncio.run(signal.wait(30)) is True


def test_wait_wakes_when_another_thread_requests_a_stop():
    signal = InterruptSignal()

    async def scenario():
        waiting = asyncio.create_task(signal.wait(30))
        await asyncio.sleep(0.01)
        thread = threading.Thread(target=signal.set)
        thread.start()
        thread.join()
        return await asyncio.wait_for(waiting, 5)

    assert asyncio.run(scenario()) is True


class StopOnAcquire:
    """A lock that requests a stop while ``wait`` registers, forcing the race it guards."""

    def __init__(self, signal):
        self._signal = signal
        self._inner = threading.Lock()

    def __enter__(self):
        self._inner.acquire()
        self._signal._flag.set()
        return self

    def __exit__(self, *exc_info):
        self._inner.release()


def test_stop_arriving_during_registration_is_not_missed():
    signal = InterruptSignal()
    signal._lock = StopOnAcquire(signal)

    assert asyncio.run(asyncio.wait_for(signal.wait(30), 5)) is True
