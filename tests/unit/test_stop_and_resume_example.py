"""The stop-and-resume example stays offline during automated verification."""

import importlib.util
import json
import logging
from pathlib import Path
from typing import ClassVar

import pytest

from mertina_agent.agent.transports.types import NormalizedResponse, ToolCall, Usage
from mertina_agent.exceptions import ModelInputError


@pytest.fixture
def example():
    logger_levels = {
        name: logging.getLogger(name).level
        for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2")
    }
    path = Path(__file__).parents[2] / "examples" / "stop_and_resume.py"
    spec = importlib.util.spec_from_file_location("stop_and_resume_example", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for name, level in logger_levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "MERTINA_LLM_BASE_URL=http://localhost:8000/v1\nMERTINA_LLM_MODEL=explicit-local-model\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def fake_client(example, monkeypatch):
    class FakeClient:
        script: ClassVar[list] = []
        requests: ClassVar[list] = []

        def __init__(self, settings):
            self.settings = settings

        async def complete(self, messages, *, tools=()):
            FakeClient.requests.append((list(messages), list(tools)))
            step = FakeClient.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step

        async def aclose(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            await self.aclose()

    FakeClient.script = []
    FakeClient.requests = []
    monkeypatch.setattr(example, "ModelClient", FakeClient)
    return FakeClient


def search_calls():
    return NormalizedResponse(
        content=None,
        tool_calls=(
            ToolCall(id="s1", name="web_search", arguments='{"query": "python"}'),
            ToolCall(id="s2", name="web_search", arguments='{"query": "node"}'),
        ),
        finish_reason="tool_calls",
        usage=Usage(1, 1, 2),
    )


def answer():
    return NormalizedResponse(content="Resumed answer.", tool_calls=(), finish_reason="stop")


def test_scenario_stops_the_first_turn_and_completes_the_second(
    example, env_file, fake_client, capsys
):
    fake_client.script = [search_calls(), answer()]

    status = example.main(["--env-file", str(env_file)])

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert status == 0
    assert report["stopped_turn"]["interrupted"] is True
    assert report["stopped_turn"]["history_roles"][-1] == "assistant"
    assert report["resumed_turn"]["completed"] is True
    assert report["resumed_turn"]["final_response"] == "Resumed answer."
    assert "stop requested while web_search runs" in output.err
    resumed_request = fake_client.requests[1][0]
    assert resumed_request[-1] == {"role": "user", "content": example.RESUME_PROMPT}


def test_scenario_reports_failure_when_the_turn_is_not_stopped(
    example, env_file, fake_client, capsys
):
    fake_client.script = [answer(), answer()]

    assert example.main(["--env-file", str(env_file)]) == 1
    assert json.loads(capsys.readouterr().out)["stopped_turn"]["interrupted"] is False


def test_missing_env_file_is_a_configuration_error(example, tmp_path):
    assert example.main(["--env-file", str(tmp_path / "missing.env")]) == 2


def test_env_file_without_explicit_endpoint_is_refused(example, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MERTINA_LOG_LEVEL=INFO\n", encoding="utf-8")

    assert example.main(["--env-file", str(env_file)]) == 2


def test_input_errors_do_not_echo_their_details(example, env_file, fake_client, capsys):
    fake_client.script = [ModelInputError("sensitive input detail")]

    assert example.main(["--env-file", str(env_file)]) == 1
    assert "sensitive input detail" not in capsys.readouterr().err


def test_keyboard_interrupt_exits_with_130(example, env_file, fake_client):
    fake_client.script = [KeyboardInterrupt()]

    assert example.main(["--env-file", str(env_file)]) == 130
