"""The package imports and exposes a version."""

import mertina


def test_version_is_exposed() -> None:
    assert mertina.__version__
