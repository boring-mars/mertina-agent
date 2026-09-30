"""Status output: the retry trace is buffered, dropped on recovery and replayed on failure."""

from typing import Any

from mertina.agent.status_output import StatusOutputMixin


class _Output(StatusOutputMixin):
    def __init__(self) -> None:
        self.printed: list[str] = []
        self._print_fn = lambda *args, **kwargs: self.printed.append(" ".join(map(str, args)))
        self.log_prefix = "[agent] "
        self.statuses: list[tuple[str, str]] = []
        self.status_callback: Any = lambda kind, message: self.statuses.append((kind, message))
        self.thinking_callback: Any = None


def test_a_status_is_printed_and_sent_to_the_callback() -> None:
    out = _Output()

    out._emit_diagnostic_status("❌ gave up")

    assert out.printed == ["[agent] ❌ gave up"]
    assert out.statuses == [("lifecycle", "❌ gave up")]


def test_the_retry_trace_waits_for_the_outcome() -> None:
    out = _Output()
    out._buffer_vprint("⚠️  Attempt 1/3 failed")
    out._buffer_diagnostic_status("⏳ Retrying in 2.0s")

    assert out.printed == []

    out._flush_status_buffer()

    assert out.printed == ["[agent] ⚠️  Attempt 1/3 failed", "[agent] ⏳ Retrying in 2.0s"]
    assert out.statuses == [("lifecycle", "⏳ Retrying in 2.0s")]
    assert out._retry_status_buffer == []


def test_a_recovered_turn_drops_the_trace() -> None:
    out = _Output()
    out._buffer_vprint("⚠️  Attempt 1/3 failed")

    out._clear_status_buffer()
    out._flush_status_buffer()

    assert out.printed == []


def test_a_failing_callback_never_breaks_the_turn() -> None:
    out = _Output()

    def broken(*args: Any) -> None:
        raise RuntimeError("driver bug")

    out.status_callback = broken
    out.thinking_callback = broken

    out._emit_diagnostic_status("still printed")
    out._emit_diagnostic_wait("⏳ waiting")

    assert out.printed == ["[agent] still printed"]
