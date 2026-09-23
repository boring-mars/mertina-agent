"""The v0.1.0 acceptance script: one full turn with a tool round trip against a fake model."""

from scripts import fake_loop


def test_the_acceptance_script_passes() -> None:
    result, completions = fake_loop.run()

    assert fake_loop.check(result, completions) == []


def test_main_exits_zero(capsys: object) -> None:
    assert fake_loop.main() == 0
