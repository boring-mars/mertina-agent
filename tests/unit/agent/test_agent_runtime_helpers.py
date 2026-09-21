"""create_openai_client builds the primary OpenAI SDK client."""

import logging
from typing import Any

import pytest
from openai import OpenAI

import mertina.run_agent
from mertina.agent.agent_runtime_helpers import _ra, create_openai_client

_KWARGS = {"api_key": "test-key", "base_url": "http://localhost:9/v1"}


class _Agent:
    def _client_log_context(self) -> str:
        return "ctx"


def _create(client_kwargs: dict[str, Any]) -> Any:
    return create_openai_client(_Agent(), client_kwargs, reason="test", shared=True)


def test_builds_an_openai_client_with_sdk_retries_off() -> None:
    client = _create(dict(_KWARGS))

    assert isinstance(client, OpenAI)
    assert client.max_retries == 0
    client.close()


def test_keeps_an_explicit_max_retries() -> None:
    client = _create({**_KWARGS, "max_retries": 3})

    assert client.max_retries == 3
    client.close()


def test_does_not_mutate_the_callers_kwargs() -> None:
    stored = dict(_KWARGS)

    _create(stored).close()

    assert stored == _KWARGS


def test_logs_the_creation_on_the_run_agent_logger(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="mertina.run_agent"):
        _create(dict(_KWARGS)).close()

    assert "OpenAI client created (test, shared=True) ctx" in caplog.messages


def test_ra_resolves_run_agent() -> None:
    assert _ra() is mertina.run_agent
