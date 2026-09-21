"""IterationBudget hands out a fixed number of iterations, thread-safely."""

import threading

from mertina.agent.iteration_budget import IterationBudget


def test_consume_succeeds_until_the_budget_is_spent() -> None:
    budget = IterationBudget(max_total=2)

    assert budget.consume()
    assert budget.consume()
    assert not budget.consume()
    assert budget.used == 2
    assert budget.remaining == 0


def test_refund_returns_an_iteration() -> None:
    budget = IterationBudget(max_total=1)
    budget.consume()

    budget.refund()

    assert budget.remaining == 1
    assert budget.consume()


def test_refund_on_an_untouched_budget_does_nothing() -> None:
    budget = IterationBudget(max_total=1)

    budget.refund()

    assert budget.used == 0
    assert budget.remaining == 1


def test_concurrent_consumers_never_exceed_the_budget() -> None:
    budget = IterationBudget(max_total=100)
    granted: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(50):
            ok = budget.consume()
            with lock:
                granted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert granted.count(True) == 100
    assert budget.used == 100
