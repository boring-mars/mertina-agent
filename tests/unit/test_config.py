"""Behavior of the environment-backed settings loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mertina_agent.config import load_settings
from mertina_agent.exceptions import ConfigurationError, MertinaError


def test_defaults_apply_when_nothing_is_set():
    settings = load_settings(env_file=None)

    assert settings.log_level == "INFO"
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_api_key is None
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_timeout_s == 60.0
    assert settings.max_iterations == 10


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MERTINA_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("MERTINA_LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("MERTINA_LLM_MODEL", "qwen3-32b")
    monkeypatch.setenv("MERTINA_LLM_TIMEOUT_S", "12.5")
    monkeypatch.setenv("MERTINA_MAX_ITERATIONS", "3")

    settings = load_settings(env_file=None)

    assert settings.log_level == "DEBUG"
    assert settings.llm_base_url == "http://localhost:8000/v1"
    assert settings.llm_model == "qwen3-32b"
    assert settings.llm_timeout_s == 12.5
    assert settings.max_iterations == 3


def test_variable_names_are_case_insensitive(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("mertina_llm_model", "gpt-4.1-mini")

    assert load_settings(env_file=None).llm_model == "gpt-4.1-mini"


def test_unknown_mertina_variables_are_ignored(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MERTINA_NOT_A_SETTING", "whatever")

    assert load_settings(env_file=None).llm_model == "gpt-4o-mini"


def test_api_key_is_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MERTINA_LLM_API_KEY", "sk-super-secret")

    settings = load_settings(env_file=None)

    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == "sk-super-secret"
    assert "sk-super-secret" not in repr(settings)


def test_env_file_fills_in_unset_variables(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("MERTINA_LLM_MODEL=from-file\n", encoding="utf-8")

    assert load_settings(env_file=env_file).llm_model == "from-file"


def test_environment_wins_over_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MERTINA_LLM_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MERTINA_LLM_MODEL", "from-environment")

    assert load_settings(env_file=env_file).llm_model == "from-environment"


def test_missing_env_file_is_not_an_error(tmp_path: Path):
    assert load_settings(env_file=tmp_path / "absent.env").llm_model == "gpt-4o-mini"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MERTINA_LOG_LEVEL", "CHATTY"),
        ("MERTINA_LLM_TIMEOUT_S", "0"),
        ("MERTINA_LLM_TIMEOUT_S", "-1"),
        ("MERTINA_LLM_TIMEOUT_S", "soon"),
        ("MERTINA_MAX_ITERATIONS", "0"),
        ("MERTINA_MAX_ITERATIONS", "many"),
    ],
)
def test_invalid_values_raise_configuration_error(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError) as error:
        load_settings(env_file=None)

    assert name.removeprefix("MERTINA_").lower() in str(error.value)


def test_configuration_error_is_a_mertina_error():
    assert issubclass(ConfigurationError, MertinaError)


def test_settings_are_immutable():
    settings = load_settings(env_file=None)

    with pytest.raises(ValidationError):
        settings.llm_model = "changed"  # type: ignore[misc]
