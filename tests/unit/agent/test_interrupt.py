"""Run-scoped interrupt signal and its context binding."""

import asyncio

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
