"""Plugin discovery: bundled directory scan, the gate, module import, and provider registration."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from mertina.agent import web_search_registry
from mertina.cli import plugins
from mertina.cli.plugins import PluginManager

_registry = web_search_registry._registry

_FAKE_PROVIDER = """
from mertina.agent.web_search_provider import WebSearchProvider


class FakeProvider(WebSearchProvider):
    name = "fake"

    def is_available(self) -> bool:
        return True


def register(ctx):
    ctx.register_web_search_provider(FakeProvider())
"""


@pytest.fixture(autouse=True)
def empty_registry() -> Iterator[None]:
    _registry.reset_for_tests()
    yield
    _registry.reset_for_tests()


@pytest.fixture
def bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty bundled plugins directory standing in for ``mertina/plugins``."""
    root = tmp_path / "bundled"
    root.mkdir()
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: root)
    return root


def _plugin(root: Path, key: str, manifest: str, init: str = _FAKE_PROVIDER) -> Path:
    plugin_dir = root.joinpath(*key.split("/"))
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(manifest, encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(init, encoding="utf-8")
    return plugin_dir


def _discover() -> PluginManager:
    manager = PluginManager()
    manager.discover_and_load()
    return manager


def test_a_bundled_backend_loads_and_registers_its_provider(bundled: Path) -> None:
    _plugin(bundled, "web/fake", "name: web-fake\nkind: backend\n")

    manager = _discover()

    loaded = manager._plugins["web/fake"]
    assert loaded.enabled and loaded.error is None
    assert loaded.module is not None and loaded.module.__name__ == "mertina_plugins.web__fake"
    provider = _registry.get_provider("fake")
    assert provider is not None
    assert web_search_registry.get_active_search_provider() is provider
    [registration] = manager._registration_order
    assert (registration.kind, registration.key, registration.plugin_key) == (
        "web_search_provider",
        "fake",
        "web/fake",
    )


def test_discovery_runs_once_unless_forced(bundled: Path) -> None:
    manager = _discover()
    _plugin(bundled, "web/fake", "name: web-fake\nkind: backend\n")

    manager.discover_and_load()
    assert manager._plugins == {}

    manager.discover_and_load(force=True)
    assert "web/fake" in manager._plugins


def test_plugins_disabled_skips_a_plugin(bundled: Path, mertina_home: Path) -> None:
    _plugin(bundled, "web/fake", "name: web-fake\nkind: backend\n")
    config = mertina_home / "config.yaml"
    config.write_text("plugins:\n  disabled: [web/fake]\n", encoding="utf-8")

    manager = _discover()

    assert manager._plugins["web/fake"].error == "disabled via config"
    assert _registry.get_provider("fake") is None


def test_a_standalone_plugin_needs_plugins_enabled(bundled: Path, mertina_home: Path) -> None:
    _plugin(bundled, "web/fake", "name: web-fake\n")

    manager = _discover()
    skipped = manager._plugins["web/fake"]
    assert not skipped.enabled
    assert _registry.get_provider("fake") is None

    (mertina_home / "config.yaml").write_text("plugins:\n  enabled: [web/fake]\n", encoding="utf-8")
    manager.discover_and_load(force=True)
    loaded = manager._plugins["web/fake"]
    assert loaded.enabled
    assert _registry.get_provider("fake") is not None


def test_a_plugin_without_register_is_recorded(bundled: Path) -> None:
    _plugin(bundled, "web/fake", "name: web-fake\nkind: backend\n", init="X = 1\n")

    manager = _discover()

    assert manager._plugins["web/fake"].error == "no register() function"


def test_a_failing_import_is_recorded_and_does_not_stop_discovery(bundled: Path) -> None:
    broken = "raise ImportError('nope')\n"
    _plugin(bundled, "web/broken", "name: web-broken\nkind: backend\n", init=broken)
    _plugin(bundled, "web/fake", "name: web-fake\nkind: backend\n")

    manager = _discover()

    assert manager._plugins["web/broken"].error == "nope"
    assert manager._plugins["web/fake"].enabled


def test_registering_the_wrong_type_is_ignored(bundled: Path) -> None:
    init = "def register(ctx):\n    ctx.register_web_search_provider(object())\n"
    _plugin(bundled, "web/fake", "name: web-fake\nkind: backend\n", init=init)

    manager = _discover()

    assert manager._plugins["web/fake"].enabled
    assert _registry.list_providers() == []
    assert manager._registration_order == []


def test_a_broken_manifest_is_skipped(bundled: Path) -> None:
    _plugin(bundled, "web/fake", "name: [unclosed\n")

    assert _discover()._plugins == {}


def test_safe_mode_skips_discovery(bundled: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plugin(bundled, "web/fake", "name: web-fake\nkind: backend\n")
    monkeypatch.setenv("MERTINA_SAFE_MODE", "1")

    assert _discover()._plugins == {}


def test_the_shipped_ddgs_plugin_registers() -> None:
    manager = _discover()

    assert manager._plugins["web/ddgs"].enabled
    provider = _registry.get_provider("ddgs")
    assert provider is not None and provider.display_name == "DuckDuckGo (ddgs)"


def test_the_manager_is_cached_per_home(
    mertina_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugins, "_plugin_manager", None)
    monkeypatch.setattr(plugins, "_plugin_managers_by_home", {})

    first = plugins.get_plugin_manager()
    assert plugins.get_plugin_manager() is first

    other_home = tmp_path / "other-home"
    other_home.mkdir()
    monkeypatch.setenv("MERTINA_HOME", str(other_home))
    assert plugins.get_plugin_manager() is not first
