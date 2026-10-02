"""create_openai_client builds the primary OpenAI SDK client; force_close_tcp_sockets aborts
its in-flight I/O from another thread."""

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from openai import OpenAI

import mertina.run_agent
from mertina.agent.agent_runtime_helpers import _ra, create_openai_client, force_close_tcp_sockets

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


# --- force_close_tcp_sockets ----------------------------------------------------------------------


class _Sock:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def settimeout(self, value: float) -> None:
        self.calls.append(f"settimeout({value})")

    def shutdown(self, how: int) -> None:
        self.calls.append("shutdown")
        if self.fail:
            raise OSError("not connected")

    def close(self) -> None:
        self.calls.append("close")


def _connection(sock: _Sock) -> Any:
    return SimpleNamespace(_network_stream=SimpleNamespace(_sock=sock))


def test_force_close_shuts_idle_and_in_flight_sockets_without_closing_them() -> None:
    idle, in_flight, proxied = _Sock(), _Sock(), _Sock(fail=True)
    pool = SimpleNamespace(
        _connections=[_connection(idle), SimpleNamespace(_connection=_connection(proxied))],
        _requests=[SimpleNamespace(connection=_connection(in_flight))],
    )
    http_client = SimpleNamespace(_transport=SimpleNamespace(_pool=pool), _mounts={})
    client = SimpleNamespace(_client=http_client)

    assert force_close_tcp_sockets(client) == 3
    for sock in (idle, in_flight, proxied):
        assert sock.calls == ["settimeout(0)", "shutdown"]


def test_force_close_finds_nothing_on_a_client_without_a_pool() -> None:
    assert force_close_tcp_sockets(SimpleNamespace()) == 0
