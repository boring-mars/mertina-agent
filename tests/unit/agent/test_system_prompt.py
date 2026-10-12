"""System prompt tiers, guidance gates and the timestamp line."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from mertina_agent.agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    GOOGLE_MODEL_OPERATIONAL_GUIDANCE,
    PARALLEL_TOOL_CALL_GUIDANCE,
    TASK_COMPLETION_GUIDANCE,
    TOOL_USE_ENFORCEMENT_GUIDANCE,
)
from mertina_agent.agent.system_prompt import build_system_prompt, build_system_prompt_parts

NOW = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)


def parts(**overrides):
    options = {"model": "test-model", "valid_tool_names": {"echo"}, "now": NOW, **overrides}
    return build_system_prompt_parts(**options)


def test_tiers_hold_identity_guidance_caller_message_and_timestamp():
    result = parts(system_message="Be brief.")

    assert result["stable"].startswith(DEFAULT_AGENT_IDENTITY)
    assert TASK_COMPLETION_GUIDANCE in result["stable"]
    assert PARALLEL_TOOL_CALL_GUIDANCE in result["stable"]
    assert result["context"] == "Be brief."
    assert result["volatile"] == (
        "Conversation started: Monday, September 21, 2026 (UTC, UTC+00:00)\nModel: test-model"
    )


def test_full_prompt_orders_tiers_and_skips_empty_ones():
    prompt = build_system_prompt(model="test-model", valid_tool_names=set(), now=NOW)

    assert prompt == f"{DEFAULT_AGENT_IDENTITY}\n\n{parts(valid_tool_names=set())['volatile']}"


def test_no_tool_guidance_without_tools():
    assert parts(valid_tool_names=set())["stable"] == DEFAULT_AGENT_IDENTITY


@pytest.mark.parametrize(
    ("model", "setting", "expected"),
    [
        ("gpt-4o-mini", "auto", True),
        ("claude-sonnet", "auto", False),
        ("claude-sonnet", True, True),
        ("gpt-4o-mini", False, False),
        ("claude-sonnet", "always", True),
        ("gpt-4o-mini", "off", False),
        ("my-custom-model", ["CUSTOM"], True),
        ("gpt-4o-mini", ["custom"], False),
    ],
)
def test_tool_use_enforcement_gate(model, setting, expected):
    stable = parts(model=model, tool_use_enforcement=setting)["stable"]

    assert (TOOL_USE_ENFORCEMENT_GUIDANCE in stable) is expected


def test_google_models_get_their_operational_guidance():
    assert GOOGLE_MODEL_OPERATIONAL_GUIDANCE.strip() in parts(model="gemini-2.5-pro")["stable"]


def test_timestamp_names_the_iana_zone_and_offset():
    now = datetime(2026, 1, 5, 8, 0, tzinfo=ZoneInfo("America/New_York"))

    volatile = parts(now=now)["volatile"]

    assert volatile.startswith(
        "Conversation started: Monday, January 05, 2026 (America/New_York, EST, UTC-05:00)"
    )


def test_naive_time_omits_the_zone_suffix():
    volatile = parts(now=datetime(2026, 9, 21, 9, 30))["volatile"]

    assert volatile.startswith("Conversation started: Monday, September 21, 2026\n")
