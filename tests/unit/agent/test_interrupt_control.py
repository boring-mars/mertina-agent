"""interrupt() / clear_interrupt(): the flag and the per-thread bits tools see."""

import threading
from collections.abc import Iterator

import pytest

from mertina.agent.interrupt_control import InterruptControlMixin
from mertina.tools.interrupt import is_thread_interrupted

_WORKER = 2**40 + 1  # an ident no live thread has


class _Agent(InterruptControlMixin):
    def __init__(self, *, execution_thread_id: int | None) -> None:
        self.quiet_mode = True
        self._execution_thread_id = execution_thread_id
        self._interrupt_requested = False
        self._interrupt_message: str | None = None
        self._tool_worker_threads: set[int | None] = {_WORKER}
        self._tool_worker_threads_lock = threading.Lock()


@pytest.fixture
def execution_thread() -> Iterator[int]:
    tid = threading.get_ident()
    yield tid
    _Agent(execution_thread_id=tid).clear_interrupt()


def test_interrupt_sets_the_flag_and_signals_every_thread(execution_thread: int) -> None:
    agent = _Agent(execution_thread_id=execution_thread)

    assert agent.interrupt("new question") is True

    assert agent._interrupt_requested is True
    assert agent._interrupt_message == "new question"
    assert is_thread_interrupted(execution_thread)
    assert is_thread_interrupted(_WORKER)


def test_clear_interrupt_drops_the_flag_and_the_bits(execution_thread: int) -> None:
    agent = _Agent(execution_thread_id=execution_thread)
    agent.interrupt()

    assert agent.clear_interrupt() is True

    assert agent._interrupt_requested is False
    assert agent._interrupt_message is None
    assert not is_thread_interrupted(execution_thread)
    assert not is_thread_interrupted(_WORKER)


def test_before_a_turn_binds_its_thread_only_the_flag_is_set() -> None:
    agent = _Agent(execution_thread_id=None)

    agent.interrupt()

    assert agent._interrupt_requested is True
    assert not is_thread_interrupted(threading.get_ident())
    agent.clear_interrupt()
