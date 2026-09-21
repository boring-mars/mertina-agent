"""ProviderTransport's default behavior, seen through a minimal subclass."""

from typing import Any

from mertina.agent.transports.base import ProviderTransport
from mertina.agent.transports.types import NormalizedResponse


class _EchoTransport(ProviderTransport):
    @property
    def api_mode(self) -> str:
        return "echo"

    def convert_messages(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        return messages

    def convert_tools(self, tools: list[dict[str, Any]]) -> Any:
        return tools

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        return {"model": model, "messages": messages}

    def normalize_response(self, response: Any, **kwargs: Any) -> NormalizedResponse:
        return NormalizedResponse(content=response, tool_calls=None, finish_reason="stop")


class _MappedTransport(_EchoTransport):
    # Same shape as the upstream transports; the base declares it as a plain attribute.
    _STOP_REASON_MAP = {"end_turn": "stop", "tool_use": "tool_calls"}  # noqa: RUF012


def test_finish_reason_passes_through_without_a_map() -> None:
    assert _EchoTransport().map_finish_reason("tool_calls") == "tool_calls"


def test_finish_reason_is_mapped_and_unknown_becomes_stop() -> None:
    transport = _MappedTransport()

    assert transport.map_finish_reason("tool_use") == "tool_calls"
    assert transport.map_finish_reason("something_new") == "stop"


def test_optional_hooks_default_to_accepting_and_no_cache_stats() -> None:
    transport = _EchoTransport()

    assert transport.validate_response(object())
    assert transport.extract_cache_stats(object()) is None
