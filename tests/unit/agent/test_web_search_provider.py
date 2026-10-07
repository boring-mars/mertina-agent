"""The web search provider interface and its env lookup."""

import pytest

from mertina.agent.provider_base import ProviderBase
from mertina.agent.web_search_provider import WebSearchProvider, get_provider_env


class _Provider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "fake"

    def is_available(self) -> bool:
        return True


def test_get_provider_env_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERTINA_TEST_SEARCH_KEY", "  secret \n")
    assert get_provider_env("MERTINA_TEST_SEARCH_KEY") == "secret"


def test_get_provider_env_is_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERTINA_TEST_SEARCH_KEY", raising=False)
    assert get_provider_env("MERTINA_TEST_SEARCH_KEY") == ""


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
