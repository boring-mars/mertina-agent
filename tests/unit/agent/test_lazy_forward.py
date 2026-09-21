"""lazy_forward builds methods that resolve their target on every call."""

import types
from typing import Any

import pytest

from mertina.agent import lazy_forward

_MODULE = "tests.unit.agent._forward_target"


@pytest.fixture(autouse=True)
def _target_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType(_MODULE)

    def greet(owner: Any, name: str) -> str:
        return f"{owner.prefix} {name}"

    def add(a: int, b: int) -> int:
        return a + b

    module.greet = greet  # type: ignore[attr-defined]
    module.add = add  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, _MODULE, module)
    return module


class _Owner:
    prefix = "hello"
    greet = lazy_forward.forward(_MODULE, "greet")
    add = lazy_forward.forward_static(_MODULE, "add")


def test_forward_passes_self_first() -> None:
    assert _Owner().greet("world") == "hello world"


def test_forward_static_drops_self() -> None:
    assert _Owner().add(2, 3) == 5
    assert _Owner.add(2, 3) == 5


def test_target_is_resolved_on_every_call(
    monkeypatch: pytest.MonkeyPatch, _target_module: types.ModuleType
) -> None:
    monkeypatch.setattr(_target_module, "greet", lambda owner, name: f"patched {name}")

    assert _Owner().greet("world") == "patched world"


def test_forwarder_is_named_after_its_target() -> None:
    assert _Owner.greet.__name__ == "greet"
    assert _Owner.greet.__doc__ == f"Forwarder — see ``{_MODULE}.greet``."


def test_lazy_attr_reads_the_module_attribute() -> None:
    assert lazy_forward.lazy_attr(_MODULE, "add")(1, 1) == 2
