"""Behavior of the environment-backed settings loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mertina_agent.config import Settings, load_settings
from mertina_agent.exceptions import ConfigurationError, MertinaError


def test_defaults_apply_when_nothing_is_set():
    settings = load_settings(env_file=None)

    assert settings.log_level == "INFO"
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_api_key is None
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_timeout_s == 60.0
    assert settings.max_iterations == 10
    assert settings.web_search_backend == "ddgs"
    assert settings.web_search_timeout_s == 30.0
    assert settings.llm_max_attempts == 3


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MERTINA_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("MERTINA_LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("MERTINA_LLM_MODEL", "qwen3-32b")
    monkeypatch.setenv("MERTINA_LLM_TIMEOUT_S", "12.5")
    monkeypatch.setenv("MERTINA_MAX_ITERATIONS", "3")
    monkeypatch.setenv("MERTINA_WEB_SEARCH_TIMEOUT_S", "5")

    settings = load_settings(env_file=None)

    assert settings.log_level == "DEBUG"
    assert settings.llm_base_url == "http://localhost:8000/v1"
    assert settings.llm_model == "qwen3-32b"
    assert settings.llm_timeout_s == 12.5
    assert settings.max_iterations == 3
    assert settings.web_search_timeout_s == 5.0


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
        ("MERTINA_LLM_TIMEOUT_S", "inf"),
        ("MERTINA_LLM_TIMEOUT_S", "nan"),
        ("MERTINA_LLM_BASE_URL", "file:///tmp/model"),
        ("MERTINA_LLM_BASE_URL", "https:///v1"),
        ("MERTINA_LLM_BASE_URL", "https://example.test:99999/v1"),
        ("MERTINA_LLM_BASE_URL", "https://user:secret@example.test/v1"),
        ("MERTINA_LLM_BASE_URL", "https://example.test/v1?route=test"),
        ("MERTINA_LLM_BASE_URL", "https://example.test/v1#fragment"),
        ("MERTINA_LLM_BASE_URL", "https://example.test/v1?"),
        ("MERTINA_LLM_BASE_URL", "https://example.test/\x7fbad"),
        ("MERTINA_LLM_MODEL", "  "),
        ("MERTINA_WEB_SEARCH_BACKEND", "google"),
        ("MERTINA_LLM_MAX_ATTEMPTS", "0"),
        ("MERTINA_WEB_SEARCH_TIMEOUT_S", "0"),
        ("MERTINA_WEB_SEARCH_TIMEOUT_S", "inf"),
    ],
)
def test_invalid_values_raise_configuration_error(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError) as error:
        load_settings(env_file=None)

    assert name.removeprefix("MERTINA_").lower() in str(error.value)


def test_invalid_settings_do_not_echo_sensitive_input(monkeypatch):
    monkeypatch.setenv("MERTINA_LLM_BASE_URL", "https://user:private-key@example.test/v1")

    with pytest.raises(ConfigurationError) as error:
        load_settings(env_file=None)

    assert "private-key" not in str(error.value)
    assert error.value.__cause__ is not None


def test_null_control_character_in_url_is_rejected_before_sdk_construction():
    with pytest.raises(ValidationError, match="llm_base_url"):
        Settings(llm_base_url="https://example.test/\x00bad")


def test_committable_env_example_never_contains_a_credential():
    settings = load_settings(env_file=Path(__file__).parents[2] / ".env.example")
    secret = settings.llm_api_key
    # Do not use a rewritten assertion on the secret value: pytest would echo
    # the credential when someone accidentally fills in the committable template.
    if secret is not None and secret.get_secret_value():
        pytest.fail("Keep credentials in ignored .env, never in .env.example.")


def test_configuration_error_is_a_mertina_error():
    assert issubclass(ConfigurationError, MertinaError)


def test_settings_are_immutable():
    settings = load_settings(env_file=None)

    with pytest.raises(ValidationError):
        settings.llm_model = "changed"  # type: ignore[misc]
