"""Iteration budget accounting, including concurrent use (Hermes race-test intent)."""

import threading

from mertina_agent.agent.iteration_budget import IterationBudget


def test_consume_succeeds_until_the_budget_is_spent():
    budget = IterationBudget(2)

    assert [budget.consume(), budget.consume(), budget.consume()] == [True, True, False]
    assert budget.used == 2
    assert budget.remaining == 0


def test_refund_returns_one_iteration():
    budget = IterationBudget(2)
    budget.consume()
    budget.consume()

    budget.refund()

    assert budget.used == 1
    assert budget.consume() is True


def test_refund_on_an_unused_budget_is_a_no_op():
    budget = IterationBudget(1)

    budget.refund()

    assert budget.used == 0
    assert budget.remaining == 1


def test_concurrent_consumers_never_exceed_the_budget():
    budget = IterationBudget(100)
    granted: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker():
        start.wait()
        results = [budget.consume() for _ in range(50)]
        with lock:
            granted.extend(results)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert granted.count(True) == 100
    assert budget.used == 100
