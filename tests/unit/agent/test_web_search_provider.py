"""The web search provider interface."""

import pytest

from mertina.agent.provider_base import ProviderBase
from mertina.agent.web_search_provider import WebSearchProvider


class _Provider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "fake"

    def is_available(self) -> bool:
        return True


def test_name_and_is_available_are_abstract() -> None:
    assert WebSearchProvider.__abstractmethods__ == {"name", "is_available"}
    assert issubclass(WebSearchProvider, ProviderBase)


def test_display_name_defaults_to_name() -> None:
    assert _Provider().display_name == "fake"


def test_search_is_supported_but_must_be_overridden() -> None:
    provider = _Provider()
    assert provider.supports_search() is True
    with pytest.raises(NotImplementedError, match="fake does not support search"):
        provider.search("query")
