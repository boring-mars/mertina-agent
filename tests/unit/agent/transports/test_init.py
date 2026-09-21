"""The transport registry maps an api_mode to a transport."""

import pytest

import mertina.agent.transports as transports
from mertina.agent.transports.chat_completions import ChatCompletionsTransport


def test_chat_completions_is_registered() -> None:
    assert isinstance(transports.get_transport("chat_completions"), ChatCompletionsTransport)


def test_unknown_api_mode_returns_none() -> None:
    assert transports.get_transport("anthropic_messages") is None


def test_each_lookup_returns_a_fresh_instance() -> None:
    first = transports.get_transport("chat_completions")
    second = transports.get_transport("chat_completions")

    assert first is not second


def test_registered_transport_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transports, "_REGISTRY", dict(transports._REGISTRY))

    transports.register_transport("echo", ChatCompletionsTransport)

    assert isinstance(transports.get_transport("echo"), ChatCompletionsTransport)
