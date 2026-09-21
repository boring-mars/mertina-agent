"""jiter_preload loads the OpenAI SDK's native JSON parser once, at package import."""

import pytest

from mertina.agent import jiter_preload


def test_importing_the_agent_package_preloads_jiter() -> None:
    assert jiter_preload._JITER_PRELOADED
    assert jiter_preload._JITER_PRELOAD_ERROR is None


def test_preloading_again_is_a_no_op() -> None:
    assert jiter_preload.preload_jiter_native_extension()


def test_a_failed_preload_is_recorded_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(name: str) -> None:
        raise ImportError(name)

    # Registering both globals makes monkeypatch restore them after the test.
    monkeypatch.setattr(jiter_preload, "_JITER_PRELOADED", False)
    monkeypatch.setattr(jiter_preload, "_JITER_PRELOAD_ERROR", None)
    monkeypatch.setattr("importlib.import_module", fail)

    assert not jiter_preload.preload_jiter_native_extension()
    assert isinstance(jiter_preload._JITER_PRELOAD_ERROR, ImportError)
