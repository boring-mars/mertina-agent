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
