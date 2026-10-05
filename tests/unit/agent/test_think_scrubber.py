"""StreamingThinkScrubber: reasoning never reaches the stream, however the deltas split it."""

import pytest

from mertina.agent.think_scrubber import StreamingThinkScrubber


def _scrub(*deltas: str) -> str:
    scrubber = StreamingThinkScrubber()
    return "".join(scrubber.feed(d) for d in deltas) + scrubber.flush()


@pytest.mark.parametrize(
    "deltas",
    [
        ("<think>plan</think>Answer.",),
        ("<thi", "nk>pl", "an</th", "ink>Answer."),
        ("<THINKING>plan</THINKING>", "Answer."),
        ("<reasoning>", "plan", "</reasoning>", "Answer."),
    ],
)
def test_a_reasoning_block_is_dropped_wherever_the_deltas_split_it(deltas: tuple[str, ...]) -> None:
    assert _scrub(*deltas) == "Answer."


def test_an_unterminated_block_is_discarded_at_the_end() -> None:
    assert _scrub("Before.\n", "<think>half a thought") == "Before.\n"


def test_an_open_tag_mid_sentence_is_prose() -> None:
    assert _scrub("Use the <think> tag to reason.") == "Use the <think> tag to reason."


def test_an_orphan_close_tag_is_stripped() -> None:
    assert _scrub("plan</think>Answer.") == "planAnswer."


def test_a_held_partial_tag_that_is_not_a_tag_is_flushed() -> None:
    scrubber = StreamingThinkScrubber()

    assert scrubber.feed("a <") == "a "
    assert scrubber.flush() == "<"


def test_reset_forgets_an_open_block() -> None:
    scrubber = StreamingThinkScrubber()
    scrubber.feed("<think>still thinking")

    scrubber.reset()

    assert scrubber.feed("Visible.") == "Visible."
