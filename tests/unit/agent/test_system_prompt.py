"""The layered system prompt: identity, tool-use guidance, the caller's message, the timestamp."""

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mertina import time as mertina_time
from mertina.agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    GOOGLE_MODEL_OPERATIONAL_GUIDANCE,
    OPENAI_MODEL_EXECUTION_GUIDANCE,
    PARALLEL_TOOL_CALL_GUIDANCE,
    TASK_COMPLETION_GUIDANCE,
    TOOL_USE_ENFORCEMENT_GUIDANCE,
)
from mertina.agent.system_prompt import (
    _model_gate,
    build_system_prompt,
    build_system_prompt_parts,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 30, 15, 0, tzinfo=SHANGHAI)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mertina_time, "now", lambda: NOW)
    monkeypatch.setattr(mertina_time, "get_timezone", lambda: SHANGHAI)


def _agent(**overrides: Any) -> SimpleNamespace:
    """The attributes ``init_agent`` sets that the prompt reads."""
    fields: dict[str, Any] = {
        "valid_tool_names": set(),
        "model": "some-model",
        "provider": "",
        "platform": None,
        "session_id": None,
        "pass_session_id": False,
        "session_start": NOW.replace(tzinfo=None),
        "_tool_use_enforcement": "auto",
    }
    return SimpleNamespace(**(fields | overrides))


# --- identity and tiers ---------------------------------------------------------------------------


def test_the_identity_is_mertina() -> None:
    assert DEFAULT_AGENT_IDENTITY.startswith("You are Mertina Agent. Be direct:")
    assert "Hermes" not in DEFAULT_AGENT_IDENTITY
    assert "Nous Research" not in DEFAULT_AGENT_IDENTITY


def test_tiers_run_identity_then_system_message_then_timestamp() -> None:
    parts = build_system_prompt_parts(_agent(), system_message="Be brief.")

    assert parts == {
        "stable": DEFAULT_AGENT_IDENTITY,
        "context": "Be brief.",
        "volatile": "Conversation started: Wednesday, September 30, 2026 "
        "(Asia/Shanghai, CST, UTC+08:00)\nModel: some-model",
    }
    assert build_system_prompt(_agent(), "Be brief.") == "\n\n".join(parts.values())


def test_without_a_system_message_the_context_tier_is_left_out() -> None:
    prompt = build_system_prompt(_agent())

    assert prompt.startswith(DEFAULT_AGENT_IDENTITY + "\n\nConversation started:")


# --- tool-use guidance ----------------------------------------------------------------------------


def test_no_guidance_without_tools() -> None:
    prompt = build_system_prompt(_agent(model="gpt-5"))

    assert TASK_COMPLETION_GUIDANCE not in prompt
    assert TOOL_USE_ENFORCEMENT_GUIDANCE not in prompt
    assert OPENAI_MODEL_EXECUTION_GUIDANCE not in prompt


@pytest.mark.parametrize(
    ("model", "enforcement", "google", "execution"),
    [
        ("gpt-5", True, False, True),
        ("deepseek-chat", True, False, True),
        ("gemini-2.5-pro", True, True, False),
        ("kimi-k2", False, False, True),
        ("claude-sonnet", False, False, False),
    ],
)
def test_guidance_with_tools_follows_the_model(
    model: str, enforcement: bool, google: bool, execution: bool
) -> None:
    prompt = build_system_prompt(_agent(valid_tool_names={"get_time"}, model=model))

    assert TASK_COMPLETION_GUIDANCE in prompt
    assert PARALLEL_TOOL_CALL_GUIDANCE in prompt
    assert (TOOL_USE_ENFORCEMENT_GUIDANCE in prompt) is enforcement
    assert (GOOGLE_MODEL_OPERATIONAL_GUIDANCE in prompt) is google
    assert (OPENAI_MODEL_EXECUTION_GUIDANCE in prompt) is execution


def test_guidance_gates_can_be_forced_and_blocks_turned_off() -> None:
    agent = _agent(
        valid_tool_names={"get_time"},
        model="claude-sonnet",
        _tool_use_enforcement=True,
        _execution_guidance=["claude"],
        _task_completion_guidance=False,
    )

    prompt = build_system_prompt(agent)

    assert TOOL_USE_ENFORCEMENT_GUIDANCE in prompt
    assert OPENAI_MODEL_EXECUTION_GUIDANCE in prompt
    assert TASK_COMPLETION_GUIDANCE not in prompt
    assert PARALLEL_TOOL_CALL_GUIDANCE in prompt


@pytest.mark.parametrize(
    ("setting", "model", "expected"),
    [
        (True, "anything", True),
        (False, "gpt-5", False),
        ("Always", "anything", True),
        ("off", "gpt-5", False),
        (["Claude"], "claude-sonnet", True),
        (["claude"], "gpt-5", False),
        ("auto", "gpt-5", True),
        ("auto", None, False),
    ],
)
def test_model_gate(setting: Any, model: str | None, expected: bool) -> None:
    assert _model_gate(setting, model, ("gpt",)) is expected


# --- timestamp line -------------------------------------------------------------------------------


def test_a_session_started_on_an_earlier_day_also_names_today() -> None:
    parts = build_system_prompt_parts(_agent(session_start=datetime(2026, 9, 28, 9, 0)))

    assert parts["volatile"].splitlines()[:2] == [
        "Conversation started: Monday, September 28, 2026 (Asia/Shanghai, CST, UTC+08:00)",
        "Today's date (as of the last context rebuild): Wednesday, September 30, 2026 "
        "— trust this over the start date for what day it is now; query tools for exact time.",
    ]


def test_a_stamped_session_id_wins_over_session_start() -> None:
    agent = _agent(session_id="20260929_080000_abc")

    assert "Conversation started: Tuesday, September 29, 2026" in build_system_prompt(agent)


def test_the_trailer_names_what_is_set_and_the_session_only_on_request() -> None:
    agent = _agent(session_id="s-1", provider="custom", platform="cli")

    assert build_system_prompt_parts(agent)["volatile"].splitlines()[1:] == [
        "Model: some-model",
        "Provider: custom",
        "Platform: cli",
    ]
    agent.pass_session_id = True
    assert "Session ID: s-1" in build_system_prompt(agent)
