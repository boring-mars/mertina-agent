"""Fixtures shared by the whole suite."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def mertina_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Mertina home per test, so the developer's own config.yaml and .env never leak in.

    The config and .env caches key on the file path, so a fresh directory also means fresh caches.
    """
    home = tmp_path_factory.mktemp("mertina-home")
    monkeypatch.setenv("MERTINA_HOME", str(home))
    return home
