"""Iteration entry: stop requests and the iteration budget."""

from mertina_agent.agent.interrupt import InterruptSignal, bind_interrupt_signal
from mertina_agent.agent.iteration_budget import IterationBudget
from mertina_agent.agent.turn_iteration_prep import begin_iteration


def begin(budget, api_call_count=0):
    return begin_iteration(
        iteration_budget=budget,
        api_call_count=api_call_count,
        interrupted=False,
        turn_exit_reason="unknown",
    )


def test_iteration_counts_and_consumes_the_call():
    budget = IterationBudget(2)

    start = begin(budget, api_call_count=1)

    assert (start.action, start.api_call_count, budget.used) == ("fallthrough", 2, 1)


def test_stop_request_ends_the_turn_before_counting():
    signal = InterruptSignal()
    signal.set()
    budget = IterationBudget(2)

    with bind_interrupt_signal(signal):
        start = begin(budget)

    assert (start.action, start.interrupted, start.turn_exit_reason) == (
        "break",
        True,
        "interrupted_by_user",
    )
    assert (start.api_call_count, budget.used) == (0, 0)


def test_exhausted_budget_ends_the_turn():
    budget = IterationBudget(1)
    budget.consume()

    start = begin(budget)

    assert (start.action, start.turn_exit_reason) == ("break", "budget_exhausted")
