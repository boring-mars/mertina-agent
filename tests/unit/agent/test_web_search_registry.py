"""The provider registry engine and the active web search provider resolution."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from mertina.agent import web_search_registry
from mertina.agent.web_search_provider import WebSearchProvider
from mertina.constants import mertina_home_key

_registry = web_search_registry._registry


class _Provider(WebSearchProvider):
    def __init__(self, name: str, available: bool = True) -> None:
        self._name = name
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available


class _Raising(_Provider):
    def is_available(self) -> bool:
        raise RuntimeError("broken provider")


@pytest.fixture(autouse=True)
def empty_registry() -> Iterator[None]:
    _registry.reset_for_tests()
    yield
    _registry.reset_for_tests()


def _write_config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_registered_providers_are_found_by_stripped_name() -> None:
    provider = _Provider("fake")
    _registry.register(provider)

    assert _registry.get_provider(" fake ") is provider
    assert _registry.list_providers() == [provider]
    assert _registry.get_provider("missing") is None


def test_re_registration_overwrites() -> None:
    first, second = _Provider("fake"), _Provider("fake")
    _registry.register(first)
    _registry.register(second)

    assert _registry.get_provider("fake") is second


def test_only_web_search_providers_with_a_name_register() -> None:
    with pytest.raises(TypeError, match="expects a WebSearchProvider"):
        _registry.register(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty string"):
        _registry.register(_Provider(" "))


def test_a_scoped_provider_is_visible_only_under_its_home(mertina_home: Path) -> None:
    provider = _Provider("fake")
    _registry.register(provider, scope="some-other-home")

    assert _registry.get_provider("fake") is None
    assert _registry.get_provider("fake", scope="some-other-home") is provider

    _registry.register(provider, scope=mertina_home_key())
    assert _registry.get_provider("fake") is provider


def test_snapshot_and_restore_put_back_the_displaced_provider() -> None:
    first, second = _Provider("fake"), _Provider("fake")
    _registry.register(first)
    previous = _registry.snapshot_registration("fake")
    _registry.register(second)

    assert _registry.restore_registration("fake", second, previous)
    assert _registry.get_provider("fake") is first
    assert not _registry.restore_registration("fake", second, previous)  # no longer current


def test_without_providers_nothing_is_active() -> None:
    assert web_search_registry.get_active_search_provider() is None


def test_the_single_available_provider_is_active() -> None:
    available = _Provider("fake")
    _registry.register(available)
    _registry.register(_Provider("offline", available=False))
    _registry.register(_Raising("broken"))

    assert web_search_registry.get_active_search_provider() is available


def test_ddgs_wins_the_legacy_walk_when_several_are_available() -> None:
    ddgs = _Provider("ddgs")
    _registry.register(_Provider("other"))
    _registry.register(ddgs)

    assert web_search_registry.get_active_search_provider() is ddgs


def test_a_configured_backend_wins_even_when_unavailable(mertina_home: Path) -> None:
    configured = _Provider("chosen", available=False)
    _registry.register(configured)
    _registry.register(_Provider("ddgs"))
    _write_config(mertina_home, "web:\n  search_backend: chosen\n")

    assert web_search_registry.get_active_search_provider() is configured


def test_web_backend_is_the_shared_fallback(mertina_home: Path) -> None:
    configured = _Provider("chosen")
    _registry.register(configured)
    _registry.register(_Provider("ddgs"))
    _write_config(mertina_home, "web:\n  backend: chosen\n")

    assert web_search_registry.get_active_search_provider() is configured


def test_an_unregistered_configured_backend_falls_back(mertina_home: Path) -> None:
    ddgs = _Provider("ddgs")
    _registry.register(ddgs)
    _write_config(mertina_home, "web:\n  search_backend: missing\n")

    assert web_search_registry.get_active_search_provider() is ddgs
