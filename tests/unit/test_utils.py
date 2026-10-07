"""atomic_replace: the plain rename and its fallbacks."""

import errno
import os
from pathlib import Path

import pytest

from mertina import utils
from mertina.utils import atomic_replace

_real_replace = os.replace


class _ContendedError(OSError):
    winerror = 32


def _files(tmp_path: Path) -> tuple[Path, Path]:
    tmp, target = tmp_path / "new.tmp", tmp_path / "target.txt"
    tmp.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    return tmp, target


def test_the_temp_file_replaces_the_target(tmp_path: Path) -> None:
    tmp, target = _files(tmp_path)

    assert atomic_replace(tmp, target) == str(target)
    assert target.read_text(encoding="utf-8") == "new"
    assert not tmp.exists()


def test_a_symlink_survives_and_its_file_is_replaced(tmp_path: Path) -> None:
    tmp, real = _files(tmp_path)
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks need privileges on this system")

    atomic_replace(tmp, link)

    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "new"


def test_a_cross_device_rename_falls_back_to_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp, target = _files(tmp_path)

    def cross_device(src: str, dst: str) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "replace", cross_device)

    atomic_replace(tmp, target)

    assert target.read_text(encoding="utf-8") == "new"
    assert not tmp.exists()


def test_a_contended_windows_rename_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp, target = _files(tmp_path)
    attempts: list[str] = []

    def held_once(src: str, dst: str) -> None:
        attempts.append(src)
        if len(attempts) == 1:
            raise _ContendedError("in use")
        _real_replace(src, dst)

    monkeypatch.setattr(utils, "_IS_WINDOWS", True)
    monkeypatch.setattr(os, "replace", held_once)

    atomic_replace(tmp, target)

    assert len(attempts) == 2
    assert target.read_text(encoding="utf-8") == "new"


def test_a_still_held_target_is_rewritten_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp, target = _files(tmp_path)
    target.write_text("a much longer old body", encoding="utf-8")

    def always_held(src: str, dst: str) -> None:
        raise _ContendedError("in use")

    monkeypatch.setattr(utils, "_IS_WINDOWS", True)
    monkeypatch.setattr(os, "replace", always_held)

    atomic_replace(tmp, target)

    assert target.read_text(encoding="utf-8") == "new"
    assert not tmp.exists()


def test_other_errors_are_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp, target = _files(tmp_path)

    def denied(src: str, dst: str) -> None:
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(os, "replace", denied)

    with pytest.raises(PermissionError):
        atomic_replace(tmp, target)
