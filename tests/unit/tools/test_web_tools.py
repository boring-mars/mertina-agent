"""The web_search tool: backend selection, dispatch to the active provider, errors, the gate."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mertina.agent import web_search_registry
from mertina.agent.web_search_provider import WebSearchProvider
from mertina.tools import web_tools
from mertina.tools.interrupt import set_interrupt
from mertina.tools.registry import registry

_registry = web_search_registry._registry


class _Provider(WebSearchProvider):
    def __init__(self, name: str, available: bool = True) -> None:
        self._name = name
        self._available = available
        self.calls: list[tuple[str, int]] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        self.calls.append((query, limit))
        hit = {"title": "T", "url": "https://t.test", "description": "d", "position": 1}
        return {"success": True, "data": {"web": [hit]}}


class _Failing(_Provider):
    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        raise RuntimeError("vendor down")


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No discovered plugins and no ddgs package, unless a test registers or patches them in."""
    _registry.reset_for_tests()
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
    yield
    _registry.reset_for_tests()


def _write_config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _search(query: str = "python", limit: Any = 5) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(web_tools.web_search_tool(query, limit))
    return result


# --- backend selection ----------------------------------------------------------------------------


def test_without_config_or_providers_the_backend_is_the_upstream_default() -> None:
    assert web_tools._get_search_backend() == "firecrawl"


def test_an_importable_ddgs_is_autodetected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)

    assert web_tools._get_search_backend() == "ddgs"


def test_an_available_plugin_provider_is_autodetected() -> None:
    _registry.register(_Provider("offline", available=False))
    _registry.register(_Provider("fake"))

    assert web_tools._get_search_backend() == "fake"


def test_search_backend_beats_backend(mertina_home: Path) -> None:
    _write_config(mertina_home, "web:\n  backend: Shared\n  search_backend: ' Mine '\n")

    assert web_tools._get_search_backend() == "mine"


def test_backend_is_used_when_search_backend_is_unset(mertina_home: Path) -> None:
    _write_config(mertina_home, "web:\n  backend: shared\n")

    assert web_tools._get_search_backend() == "shared"


# --- web_search_tool ------------------------------------------------------------------------------


def test_the_configured_provider_answers() -> None:
    provider = _Provider("fake")
    _registry.register(provider)

    result = _search("python", 3)

    assert provider.calls == [("python", 3)]
    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://t.test"


@pytest.mark.parametrize(("limit", "sent"), [(0, 1), (500, 100), ("7", 7), ("many", 5), (None, 5)])
def test_the_limit_is_clamped(limit: Any, sent: int) -> None:
    provider = _Provider("fake")
    _registry.register(provider)

    _search(limit=limit)

    assert provider.calls == [("python", sent)]


def test_an_interrupt_stops_before_the_search() -> None:
    provider = _Provider("fake")
    _registry.register(provider)
    set_interrupt(True)
    try:
        result = _search()
    finally:
        set_interrupt(False)

    assert result == {"error": "Interrupted", "success": False}
    assert provider.calls == []


def test_without_any_provider_the_error_says_so() -> None:
    result = _search()

    assert result["success"] is False
    assert result["error"] == (
        "No web search provider configured. Set web.search_backend in config.yaml."
    )


def test_an_unregistered_selection_is_not_silently_replaced(mertina_home: Path) -> None:
    fallback = _Provider("fake")
    _registry.register(fallback)
    _write_config(mertina_home, "web:\n  search_backend: missing\n")

    result = _search()

    assert fallback.calls == []
    assert result["success"] is False
    assert "web is configured to use 'missing'" in result["error"]
    assert "no registered web search provider has that name" in result["error"]


def test_a_disabled_bundled_plugin_is_named(
    mertina_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mertina.cli.plugins import _ensure_plugins_discovered

    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", _ensure_plugins_discovered)
    _write_config(mertina_home, "web:\n  search_backend: ddgs\nplugins:\n  disabled: [web/ddgs]\n")

    result = _search()

    assert result["success"] is False
    assert result["error"] == (
        "web.search_backend is set to 'ddgs', but its plugin ('web/ddgs') is disabled in config. "
        "Re-enable it by removing it from plugins.disabled."
    )


def test_a_raising_provider_becomes_a_tool_error() -> None:
    _registry.register(_Failing("fake"))

    assert _search() == {"error": "Error searching web: vendor down"}


# --- check_web_api_key and registration -----------------------------------------------------------


def test_the_gate_is_open_with_ddgs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)

    assert web_tools.check_web_api_key()


def test_the_gate_is_closed_without_a_provider() -> None:
    assert not web_tools.check_web_api_key()


def test_the_gate_follows_the_active_plugin_provider() -> None:
    _registry.register(_Provider("fake", available=False))
    assert not web_tools.check_web_api_key()

    _registry.register(_Provider("fake"))
    assert web_tools.check_web_api_key()


def test_a_configured_plugin_provider_opens_the_gate(mertina_home: Path) -> None:
    _registry.register(_Provider("fake"))
    _write_config(mertina_home, "web:\n  backend: fake\n")

    assert web_tools._is_backend_available("fake")
    assert web_tools.check_web_api_key()


def test_web_search_is_registered_behind_the_gate() -> None:
    entry = registry.get_entry("web_search")

    assert entry is not None
    assert entry.toolset == "web"
    assert entry.check_fn is web_tools.check_web_api_key
    assert entry.schema["parameters"]["required"] == ["query"]
