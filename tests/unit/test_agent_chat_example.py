"""The agent example stays offline during automated verification."""

import importlib.util
import json
import logging
from pathlib import Path
from typing import ClassVar

import pytest

from mertina_agent.agent.events import RetryScheduled
from mertina_agent.agent.transports.types import NormalizedResponse, ToolCall, Usage
from mertina_agent.exceptions import ModelInputError, ModelRequestError


@pytest.fixture
def example():
    logger_levels = {
        name: logging.getLogger(name).level
        for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2")
    }
    path = Path(__file__).parents[2] / "examples" / "agent_chat.py"
    spec = importlib.util.spec_from_file_location("agent_chat_example", path)
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

        async def stream(self, messages, *, tools=(), on_text_delta=None, on_tool_started=None):
            del on_tool_started
            response = await self.complete(messages, tools=tools)
            if response.content and on_text_delta is not None:
                on_text_delta(response.content)
            return response

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


def tool_call_response():
    call = ToolCall(id="call_1", name="get_current_time", arguments="{}")
    return NormalizedResponse(
        content=None, tool_calls=(call,), finish_reason="tool_calls", usage=Usage(5, 1, 6)
    )


def answer(text="It is noon."):
    return NormalizedResponse(
        content=text, tool_calls=(), finish_reason="stop", usage=Usage(7, 2, 9)
    )


def test_missing_env_file_is_a_configuration_error(example, tmp_path, capsys):
    status = example.main(["--env-file", str(tmp_path / "missing.env")])

    assert status == 2
    assert "Configuration error" in capsys.readouterr().err


def test_env_file_without_explicit_endpoint_is_refused(example, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MERTINA_LOG_LEVEL=INFO\n", encoding="utf-8")

    assert example.main(["--env-file", str(env_file)]) == 2


def test_turn_runs_the_demo_tool_and_prints_a_summary(
    example, explicit_env_file, fake_client, capsys
):
    fake_client.script = [tool_call_response(), answer()]

    status = example.main(["--env-file", str(explicit_env_file), "What time is it?"])

    output = capsys.readouterr()
    summary = json.loads(output.out)
    assert status == 0
    assert summary["final_response"] == "It is noon."
    assert summary["completed"] is True
    assert summary["tools_used"] == ["get_current_time"]
    assert summary["usage"] == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    assert "-> tool get_current_time (call_1)" in output.err
    assert "<- tool get_current_time ok" in output.err
    tool_result = fake_client.requests[1][0][-1]
    assert "now" in json.loads(tool_result["content"])
    offered = sorted(tool["function"]["name"] for tool in fake_client.requests[0][1])
    assert offered == ["get_current_time", "web_search"]


def test_output_escapes_terminal_control_characters(
    example, explicit_env_file, fake_client, capsys
):
    fake_client.script = [answer("red \x1b[31m text")]

    example.main(["--env-file", str(explicit_env_file)])

    output = capsys.readouterr()
    assert "\x1b" not in output.out
    assert "\x1b" not in output.err
    assert "red \\x1b[31m text" in output.err


def test_failed_turn_exits_with_one_and_still_prints_the_summary(
    example, explicit_env_file, fake_client, capsys
):
    fake_client.script = [ModelRequestError("Model endpoint returned HTTP 503", kind="http")]

    status = example.main(["--env-file", str(explicit_env_file)])

    assert status == 1
    assert json.loads(capsys.readouterr().out)["turn_exit_reason"] == "model_request_failed"


def test_input_errors_do_not_echo_their_details(example, explicit_env_file, fake_client, capsys):
    fake_client.script = [ModelInputError("sensitive input detail")]

    status = example.main(["--env-file", str(explicit_env_file)])

    assert status == 1
    assert "sensitive input detail" not in capsys.readouterr().err


def test_retries_are_announced_on_stderr(example, capsys):
    example._report_event(RetryScheduled(attempt=1, max_attempts=3, wait_s=2.5, reason="http 503"))

    assert capsys.readouterr().err == "\n[retrying in 2.5s: http 503]\n"


def test_keyboard_interrupt_exits_with_130(example, explicit_env_file, fake_client):
    fake_client.script = [KeyboardInterrupt()]

    assert example.main(["--env-file", str(explicit_env_file)]) == 130


def test_sdk_loggers_are_quietened(example, explicit_env_file, fake_client):
    fake_client.script = [answer()]
    logging.getLogger("httpx").setLevel(logging.DEBUG)

    example.main(["--env-file", str(explicit_env_file)])

    assert logging.getLogger("httpx").level == logging.WARNING
