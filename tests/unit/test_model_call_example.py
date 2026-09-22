"""The explicit smoke example stays offline during automated verification."""

import importlib.util
import json
import logging
from pathlib import Path

import pytest

from mertina_agent.agent.transports.types import NormalizedResponse, ToolCall, Usage
from mertina_agent.exceptions import (
    ConfigurationError,
    ModelInputError,
    ModelRequestError,
    ModelResponseError,
)


@pytest.fixture
def example():
    logger_levels = {
        name: logging.getLogger(name).level
        for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2")
    }
    path = Path(__file__).parents[2] / "examples" / "model_call.py"
    spec = importlib.util.spec_from_file_location("model_call_example", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for name, level in logger_levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def explicit_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "MERTINA_LLM_BASE_URL=http://localhost:8000/v1\n"
        "MERTINA_LLM_MODEL=explicit-local-model\n"
        "MERTINA_LLM_API_KEY=\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def fake_client(example, monkeypatch):
    instances = []

    class FakeClient:
        response = NormalizedResponse(
            content="Hello\n\x1b[31m", tool_calls=(), finish_reason="stop", usage=None
        )
        failure = None

        def __init__(self, settings):
            self.settings = settings
            self.calls = []
            self.closed = False
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc_value, _traceback):
            self.closed = True

        async def complete(self, messages, *, tools):
            self.calls.append((messages, tools))
            if self.failure is not None:
                raise self.failure
            return self.response

    monkeypatch.setattr(example, "ModelClient", FakeClient)
    return FakeClient, instances


def test_help_does_not_require_configuration_or_create_client(example, fake_client, capsys):
    _, instances = fake_client

    with pytest.raises(SystemExit) as error:
        example.main(["--help"])

    assert error.value.code == 0
    assert "--tools" in capsys.readouterr().out
    assert instances == []


def test_missing_env_file_fails_without_constructing_client(example, fake_client, tmp_path, capsys):
    _, instances = fake_client

    assert example.main(["--env-file", str(tmp_path / "missing.env")]) == 2

    assert instances == []
    assert "existing env file" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    ["", "MERTINA_LLM_MODEL=only-model\n", "MERTINA_LLM_BASE_URL=http://localhost:8000/v1\n"],
)
def test_partial_configuration_cannot_silently_use_defaults(
    content, example, fake_client, tmp_path, capsys
):
    _, instances = fake_client
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")

    assert example.main(["--env-file", str(path)]) == 2

    assert instances == []
    assert "explicit" in capsys.readouterr().err


def test_no_key_endpoint_sends_once_and_outputs_escaped_normalized_result(
    example, fake_client, explicit_env_file, capsys
):
    _, instances = fake_client

    assert example.main(["--env-file", str(explicit_env_file)]) == 0

    client = instances[0]
    assert client.closed
    assert client.settings.llm_api_key.get_secret_value() == ""
    assert client.calls == [([{"role": "user", "content": example.DEFAULT_PROMPT}], [])]
    output = capsys.readouterr()
    assert output.err == ""
    assert "\x1b" not in output.out
    assert example.DEFAULT_PROMPT not in output.out
    assert json.loads(output.out)["content"] == "Hello\n\x1b[31m"


def test_explicit_environment_overrides_file_defaults(
    example, fake_client, tmp_path, monkeypatch, capsys
):
    _, instances = fake_client
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("MERTINA_LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("MERTINA_LLM_MODEL", "from-environment")

    assert example.main(["--env-file", str(path)]) == 0

    assert instances[0].settings.llm_model == "from-environment"
    assert instances[0].settings.llm_base_url == "http://localhost:1234/v1"
    assert capsys.readouterr().err == ""


def test_tools_are_displayed_without_a_follow_up_or_execution(
    example, fake_client, explicit_env_file, capsys
):
    factory, instances = fake_client
    factory.response = NormalizedResponse(
        content=None,
        tool_calls=(ToolCall(id="call-1", name="echo", arguments='{"text":"hello"}'),),
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
    )

    assert example.main(["--env-file", str(explicit_env_file), "--tools"]) == 0

    assert len(instances) == 1
    assert len(instances[0].calls) == 1
    messages, tools = instances[0].calls[0]
    assert messages == [{"role": "user", "content": example.TOOL_PROMPT}]
    assert tools[0]["function"]["name"] == "echo"
    output = json.loads(capsys.readouterr().out)
    assert output["tool_calls"][0]["arguments"] == '{"text":"hello"}'
    assert output["usage"]["total_tokens"] == 3
    assert instances[0].closed


@pytest.mark.parametrize("error_type", [ModelInputError, ModelResponseError, ConfigurationError])
def test_model_errors_do_not_print_sensitive_messages_or_causes(
    error_type, example, fake_client, explicit_env_file, capsys
):
    factory, instances = fake_client
    failure = error_type("secret credential and complete prompt")
    failure.__cause__ = RuntimeError("raw provider error body")
    factory.failure = failure

    assert example.main(["--env-file", str(explicit_env_file)]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "secret" not in output.err
    assert "prompt" not in output.err
    assert "provider" not in output.err
    assert instances[0].closed
    assert len(instances[0].calls) == 1


def test_configuration_errors_do_not_print_raw_settings(
    example, fake_client, explicit_env_file, monkeypatch, capsys
):
    _, instances = fake_client

    def invalid_settings(**_kwargs):
        message = "secret credential accidentally included in validation error"
        raise ConfigurationError(message)

    monkeypatch.setattr(example, "load_settings", invalid_settings)

    assert example.main(["--env-file", str(explicit_env_file)]) == 2

    assert instances == []
    assert "secret" not in capsys.readouterr().err


def test_http_failure_displays_only_controlled_metadata(
    example, fake_client, explicit_env_file, capsys
):
    factory, instances = fake_client
    factory.failure = ModelRequestError(
        "secret provider error body", kind="http", status_code=429, retry_after="sensitive header"
    )

    assert example.main(["--env-file", str(explicit_env_file)]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Model request failed (http, HTTP 429).\n"
    assert instances[0].closed
    assert len(instances[0].calls) == 1


def test_env_file_read_failure_does_not_print_its_path_or_raw_error(
    example, fake_client, explicit_env_file, monkeypatch, capsys
):
    _, instances = fake_client

    def unreadable_settings(**_kwargs):
        message = "secret file path in read failure"
        raise OSError(message)

    monkeypatch.setattr(example, "load_settings", unreadable_settings)

    assert example.main(["--env-file", str(explicit_env_file)]) == 2

    assert instances == []
    assert "secret" not in capsys.readouterr().err


def test_invalid_env_file_encoding_fails_without_dispatch(example, fake_client, tmp_path, capsys):
    _, instances = fake_client
    path = tmp_path / "secret.env"
    path.write_bytes(b"MERTINA_LLM_API_KEY=private\xffcredential\n")

    assert example.main(["--env-file", str(path)]) == 2

    assert instances == []
    output = capsys.readouterr()
    assert output.out == ""
    assert "private" not in output.err
    assert "secret" not in output.err


def test_keyboard_interrupt_exits_cleanly_without_printing_raw_error(
    example, fake_client, explicit_env_file, capsys
):
    factory, instances = fake_client
    factory.failure = KeyboardInterrupt("secret input")

    assert example.main(["--env-file", str(explicit_env_file)]) == 130

    assert capsys.readouterr().err == "Model call interrupted.\n"
    assert instances[0].closed


def test_sdk_debug_logging_is_suppressed_for_explicit_example(
    example, fake_client, explicit_env_file, capsys
):
    _, instances = fake_client
    logger = logging.getLogger("openai")
    logger.setLevel(logging.DEBUG)

    assert example.main(["--env-file", str(explicit_env_file)]) == 0

    assert logger.getEffectiveLevel() == logging.WARNING
    assert len(instances[0].calls) == 1
    assert capsys.readouterr().err == ""
