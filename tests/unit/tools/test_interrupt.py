"""Per-thread interrupt bits: a stop aimed at one agent's thread never reaches another's tools."""

import threading

from mertina.tools.interrupt import is_interrupted, is_thread_interrupted, set_interrupt


def test_the_bit_defaults_to_the_current_thread() -> None:
    set_interrupt(True)
    try:
        assert is_interrupted()
        assert is_thread_interrupted(threading.get_ident())
    finally:
        set_interrupt(False)

    assert not is_interrupted()


def test_a_bit_on_another_thread_is_invisible_here() -> None:
    seen: list[bool] = []
    target = threading.Event()
    release = threading.Event()

    def tool() -> None:
        target.set()
        release.wait(timeout=5)
        seen.append(is_interrupted())

    worker = threading.Thread(target=tool)
    worker.start()
    target.wait(timeout=5)
    set_interrupt(True, worker.ident)
    try:
        assert not is_interrupted()
        release.set()
        worker.join(timeout=5)
    finally:
        set_interrupt(False, worker.ident)

    assert seen == [True]
    assert not is_thread_interrupted(worker.ident)


def test_no_thread_is_never_interrupted() -> None:
    assert not is_thread_interrupted(None)
