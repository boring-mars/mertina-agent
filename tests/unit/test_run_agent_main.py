"""run_agent.main(): the endpoint comes from the arguments, then config.yaml, then the env."""

from pathlib import Path
from typing import Any

import pytest

from mertina import run_agent
from mertina.run_agent import _endpoint_from_config


@pytest.fixture(autouse=True)
def no_openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _write_config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_nothing_configured_leaves_the_arguments_empty() -> None:
    assert _endpoint_from_config("", "", None) == ("", "", None)


def test_a_bare_model_name(mertina_home: Path) -> None:
    _write_config(mertina_home, "model: gpt-x\n")

    assert _endpoint_from_config("", "", None) == ("gpt-x", "", None)


def test_a_model_mapping(mertina_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", "sk-from-ref")
    _write_config(
        mertina_home,
        "model:\n  default: gpt-x\n  base_url: http://llm.test/v1\n  api_key: ${MY_KEY}\n",
    )

    assert _endpoint_from_config("", "", None) == ("gpt-x", "http://llm.test/v1", "sk-from-ref")


def test_model_is_an_alias_for_default(mertina_home: Path) -> None:
    _write_config(mertina_home, "model:\n  model: gpt-y\n")

    assert _endpoint_from_config("", "", None)[0] == "gpt-y"


def test_the_environment_fills_what_config_leaves_out(
    mertina_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(mertina_home, "model: gpt-x\n")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env.test/v1")
    (mertina_home / ".env").write_text("OPENAI_API_KEY=sk-dotenv\n", encoding="utf-8")

    assert _endpoint_from_config("", "", None) == ("gpt-x", "http://env.test/v1", "sk-dotenv")


def test_config_beats_the_environment(mertina_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(mertina_home, "model:\n  base_url: http://config.test/v1\n  api_key: sk-config\n")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    assert _endpoint_from_config("", "", None) == ("", "http://config.test/v1", "sk-config")


def test_arguments_beat_config(mertina_home: Path) -> None:
    _write_config(
        mertina_home, "model:\n  default: gpt-x\n  base_url: http://config.test/v1\n  api_key: k\n"
    )

    assert _endpoint_from_config("m", "http://arg.test/v1", "sk-arg") == (
        "m",
        "http://arg.test/v1",
        "sk-arg",
    )


def test_main_builds_the_agent_on_the_configured_endpoint(
    mertina_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(
        mertina_home, "model:\n  default: gpt-x\n  base_url: http://llm.test/v1\n  api_key: k\n"
    )
    seen: dict[str, Any] = {}

    class _Agent:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        def run_conversation(self, query: str) -> dict[str, Any]:
            return {"completed": True, "api_calls": 1, "messages": [], "final_response": "hi"}

    monkeypatch.setattr(run_agent, "AIAgent", _Agent)

    run_agent.main("hello")

    assert (seen["model"], seen["base_url"], seen["api_key"]) == (
        "gpt-x",
        "http://llm.test/v1",
        "k",
    )
    assert "hi" in capsys.readouterr().out


def test_main_loads_the_dotenv_before_reading_config(
    mertina_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mertina.cli import env_loader

    monkeypatch.setattr(env_loader, "_DOTENV_PUBLISHED", {})
    monkeypatch.setenv("MERTINA_T_KEY", "placeholder")  # restored after the test
    (mertina_home / ".env").write_text("MERTINA_T_KEY=sk-dotenv\n", encoding="utf-8")
    _write_config(
        mertina_home,
        "model:\n  default: gpt-x\n  base_url: http://llm.test/v1\n  api_key: ${MERTINA_T_KEY}\n",
    )
    seen: dict[str, Any] = {}

    class _Agent:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        def run_conversation(self, query: str) -> dict[str, Any]:
            return {"completed": True, "api_calls": 1, "messages": [], "final_response": ""}

    monkeypatch.setattr(run_agent, "AIAgent", _Agent)

    run_agent.main("hello")

    assert seen["api_key"] == "sk-dotenv"
