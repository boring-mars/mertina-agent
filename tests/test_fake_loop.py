"""The v0.1 acceptance script: a streamed tool round trip, and a stop the next turn recovers
from."""

from scripts import fake_loop


def test_the_round_trip_passes() -> None:
    assert fake_loop.check(fake_loop.run()) == []


def test_the_stop_and_continue_passes() -> None:
    assert fake_loop.check_stop(fake_loop.run_stop()) == []


def test_main_exits_zero(capsys: object) -> None:
    assert fake_loop.main() == 0
