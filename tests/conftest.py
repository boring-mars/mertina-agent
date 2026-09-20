"""Fixtures shared by the whole test suite."""

import os

import pytest

ENV_PREFIX = "MERTINA_"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's own MERTINA_* variables from every test.

    Without this, a populated local shell or ``.env`` would leak into the tests
    and make their outcome depend on the machine they run on.
    """
    for name in [key for key in os.environ if key.startswith(ENV_PREFIX)]:
        monkeypatch.delenv(name)
