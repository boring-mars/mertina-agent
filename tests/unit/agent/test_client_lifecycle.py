"""ClientLifecycleMixin: building, closing and recreating the shared OpenAI client."""

from typing import Any

import pytest
from openai import OpenAI

from mertina.agent.client_lifecycle import ClientLifecycleMixin


class _FakeClient:
    def __init__(self, *, closed: bool = False) -> None:
        self.closed = closed

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


class _Agent(ClientLifecycleMixin):
    def __init__(self, client: Any = None) -> None:
        self.client = client
        self._client_kwargs = {"api_key": "test-key", "base_url": "http://localhost:9/v1"}


# --- _create_openai_client ------------------------------------------------------------------------


def test_create_forwards_to_agent_runtime_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake(agent: Any, client_kwargs: dict[str, Any], *, reason: str, shared: bool) -> str:
        calls.append((agent, client_kwargs, reason, shared))
        return "client"

    monkeypatch.setattr("mertina.agent.agent_runtime_helpers.create_openai_client", fake)
    agent = _Agent()

    result = agent._create_openai_client({"api_key": "k"}, reason="test", shared=False)

    assert result == "client"
    assert calls == [(agent, {"api_key": "k"}, "test", False)]


# --- _is_openai_client_closed ---------------------------------------------------------------------


def test_closed_check_calls_an_is_closed_method() -> None:
    assert ClientLifecycleMixin._is_openai_client_closed(_FakeClient(closed=True))
    assert not ClientLifecycleMixin._is_openai_client_closed(_FakeClient(closed=False))


def test_closed_check_reads_an_is_closed_property() -> None:
    class _HttpxLike:
        is_closed = True

    assert ClientLifecycleMixin._is_openai_client_closed(_HttpxLike())


def test_closed_check_falls_back_to_the_wrapped_http_client() -> None:
    class _Wrapper:
        def __init__(self) -> None:
            self._client = _FakeClient(closed=True)
            self._client.is_closed = True  # type: ignore[method-assign,assignment]  # httpx shape

    assert ClientLifecycleMixin._is_openai_client_closed(_Wrapper())


def test_a_real_client_is_open_until_closed() -> None:
    client = OpenAI(api_key="test-key", base_url="http://localhost:9/v1")

    assert not ClientLifecycleMixin._is_openai_client_closed(client)
    client.close()
    assert ClientLifecycleMixin._is_openai_client_closed(client)


# --- _ensure_primary_openai_client ----------------------------------------------------------------


def test_ensure_returns_the_open_client_as_is() -> None:
    client = _FakeClient()
    agent = _Agent(client)

    assert agent._ensure_primary_openai_client(reason="test") is client


def test_ensure_replaces_a_closed_client() -> None:
    old = _FakeClient(closed=True)
    agent = _Agent(old)

    new = agent._ensure_primary_openai_client(reason="test")

    assert isinstance(new, OpenAI)
    assert agent.client is new
    new.close()


def test_ensure_raises_when_the_client_cannot_be_rebuilt(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _Agent(_FakeClient(closed=True))

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("bad kwargs")

    monkeypatch.setattr(agent, "_create_openai_client", fail)

    with pytest.raises(RuntimeError, match="Failed to recreate closed OpenAI client"):
        agent._ensure_primary_openai_client(reason="test")


def test_close_tolerates_none_and_failing_clients() -> None:
    class _Broken:
        def close(self) -> None:
            raise OSError("already gone")

    agent = _Agent()

    agent._close_openai_client(None, reason="test", shared=False)
    agent._close_openai_client(_Broken(), reason="test", shared=False)


# --- per-request clients --------------------------------------------------------------------------


class _RequestAgent(_Agent):
    """Builds a fresh ``_FakeClient`` per request client and records what was shut down."""

    def __init__(self) -> None:
        super().__init__(_FakeClient())
        self.built: list[dict[str, Any]] = []
        self.shut: list[Any] = []

    def _create_openai_client(
        self, client_kwargs: dict[str, Any], *, reason: str, shared: bool
    ) -> _FakeClient:
        self.built.append(client_kwargs)
        return _FakeClient()

    def _force_close_tcp_sockets(self, client: Any) -> int:
        self.shut.append(client)
        return 1


def test_a_request_client_has_sdk_retries_off_and_is_reused_after_a_clean_finish() -> None:
    agent = _RequestAgent()

    first = agent._create_request_openai_client(reason="test")
    agent._close_request_openai_client(first, reason="request_complete")
    second = agent._create_request_openai_client(reason="test")

    assert second is first
    assert not first.closed
    assert [kwargs["max_retries"] for kwargs in agent.built] == [0]
    assert agent._client_kwargs.get("max_retries") is None


def test_a_failed_request_closes_its_client() -> None:
    agent = _RequestAgent()

    first = agent._create_request_openai_client(reason="test")
    agent._close_request_openai_client(first, reason="request_error_cleanup")
    second = agent._create_request_openai_client(reason="test")

    assert first.closed
    assert second is not first


def test_a_client_in_use_is_not_handed_out_twice() -> None:
    agent = _RequestAgent()

    first = agent._create_request_openai_client(reason="test")
    second = agent._create_request_openai_client(reason="test")

    assert second is not first
    agent._close_request_openai_client(second, reason="request_complete")
    assert second.closed  # untracked: fully closed, never cached


def test_an_aborted_client_is_shut_down_not_closed_and_never_reused() -> None:
    agent = _RequestAgent()
    client = agent._create_request_openai_client(reason="test")

    agent._abort_request_openai_client(client, reason="interrupt_abort")

    assert agent.shut == [client]
    assert not client.closed  # the owner thread closes it
    agent._close_request_openai_client(client, reason="request_complete")
    assert client.closed
    assert agent._create_request_openai_client(reason="test") is not client


def test_aborting_without_a_client_does_nothing() -> None:
    agent = _RequestAgent()

    agent._abort_request_openai_client(None, reason="interrupt_abort")

    assert agent.shut == []
