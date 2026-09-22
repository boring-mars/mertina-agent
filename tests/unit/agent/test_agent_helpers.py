"""Helpers AIAgent reaches: think stripping, reasoning extraction, echo-back rules, prompt, copy."""

from types import SimpleNamespace
from typing import Any

import pytest

from mertina.agent.agent_runtime_helpers import extract_reasoning, strip_think_blocks
from mertina.agent.message_content import flatten_message_text
from mertina.agent.message_sanitization import (
    apply_reasoning_content_policy,
    matches_reasoning_echo_family,
)
from mertina.agent.system_prompt import build_system_prompt
from mertina.agent.turn_failure_copy import site_copy
from mertina.utils import base_url_host_matches, base_url_hostname

# --- strip_think_blocks / extract_reasoning -------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "visible"),
    [
        ("<think>plan</think>Answer.", "Answer."),
        ("<THINKING>x</THINKING>Answer.", "Answer."),
        ("Answer.\n<think>cut off mid-thought", "Answer."),
        ("<tool_call>{}</tool_call>Done.", "Done."),
        ("An orphan <think> tag goes.", "An orphan tag goes."),
        ("Call <function> in prose stays.", "Call <function> in prose stays."),
        ("", ""),
    ],
)
def test_strip_think_blocks(content: str, visible: str) -> None:
    assert strip_think_blocks(None, content).strip() == visible


def test_extract_reasoning_prefers_structured_fields_over_inline_blocks() -> None:
    structured = SimpleNamespace(
        reasoning_content="from the field", content="<think>inline</think>"
    )
    inline = SimpleNamespace(content="<think>inline</think>visible")

    assert extract_reasoning(None, structured) == "from the field"
    assert extract_reasoning(None, inline) == "inline"
    assert extract_reasoning(None, SimpleNamespace(content="plain")) is None


def test_flatten_message_text_keeps_text_parts_only() -> None:
    parts: list[Any] = [
        {"type": "text", "text": "a"},
        {"type": "image_url", "image_url": {}},
        "b",
    ]

    assert flatten_message_text(parts) == "a\nb"
    assert flatten_message_text(None) == ""


# --- reasoning_content echo-back ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "provider", "model", "base_url", "expected"),
    [
        ("deepseek", "", "", "https://api.deepseek.com/v1", True),
        ("deepseek", "openrouter", "deepseek/deepseek-v4", "https://openrouter.ai/api/v1", True),
        ("kimi", "", None, "https://api.moonshot.ai/v1", True),
        ("mimo", "xiaomi", "", "https://example.test", True),
        ("deepseek", "", "gpt-5", "https://api.openai.com/v1", False),
    ],
)
def test_echo_families(
    family: str, provider: str, model: str | None, base_url: str, expected: bool
) -> None:
    assert matches_reasoning_echo_family(family, provider, model, base_url) is expected


def test_reasoning_content_is_stripped_on_the_strict_side_and_padded_on_the_echo_side() -> None:
    source = {"role": "assistant", "content": "", "tool_calls": [{}], "reasoning": "r"}

    strict = {"role": "assistant", "reasoning_content": "leftover"}
    apply_reasoning_content_policy(source, strict, needs_thinking_pad=False)
    echo: dict[str, Any] = {"role": "assistant"}
    apply_reasoning_content_policy(source, echo, needs_thinking_pad=True)

    assert "reasoning_content" not in strict
    assert echo["reasoning_content"] == " "


def test_base_url_host_matches_the_domain_and_its_subdomains_only() -> None:
    assert base_url_host_matches("https://api.deepseek.com/v1", "deepseek.com")
    assert base_url_host_matches("api.deepseek.com", "api.deepseek.com")
    assert not base_url_host_matches("https://evil-deepseek.com", "deepseek.com")
    assert base_url_hostname("HTTPS://API.Example.COM:8080/x") == "api.example.com"


# --- prompt and copy ------------------------------------------------------------------------------


def test_build_system_prompt_is_the_callers_system_message() -> None:
    assert build_system_prompt(None, "Be brief.") == "Be brief."
    assert build_system_prompt(None) == ""


def test_site_copy_fills_the_fields_it_is_given() -> None:
    text = site_copy("max_iterations_no_summary", limit=5)

    assert "(5 tool calls)" in text
