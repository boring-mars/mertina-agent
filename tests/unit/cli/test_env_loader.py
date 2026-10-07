"""Loading the Mertina .env into the process environment."""

import codecs
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from mertina.cli import env_loader
from mertina.cli.config import _sanitize_env_lines, load_config
from mertina.cli.env_loader import load_mertina_dotenv


@pytest.fixture(autouse=True)
def restore_environ(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The loader writes os.environ and keeps per-process records; undo both after each test."""
    saved = dict(os.environ)
    monkeypatch.setattr(env_loader, "_DOTENV_PUBLISHED", {})
    monkeypatch.setattr(env_loader, "_WARNED_KEYS", set())
    monkeypatch.setattr(env_loader, "_WARNED_UTF32_PATHS", set())
    yield
    os.environ.clear()
    os.environ.update(saved)


def _env(home: Path, data: bytes | str) -> Path:
    path = home / ".env"
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8", newline="")
    else:
        path.write_bytes(data)
    return path


def test_without_a_dotenv_nothing_loads() -> None:
    assert load_mertina_dotenv() == []


def test_the_home_dotenv_loads_and_beats_the_shell(
    mertina_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERTINA_T_SHELL", "from-shell")
    path = _env(mertina_home, "MERTINA_T_A=1\nexport MERTINA_T_SHELL=from-dotenv\n# a comment\n")

    assert load_mertina_dotenv() == [path]
    assert os.environ["MERTINA_T_A"] == "1"
    assert os.environ["MERTINA_T_SHELL"] == "from-dotenv"


def test_an_explicit_home(tmp_path: Path) -> None:
    path = _env(tmp_path, "MERTINA_T_A=elsewhere\n")

    assert load_mertina_dotenv(mertina_home=tmp_path) == [path]
    assert os.environ["MERTINA_T_A"] == "elsewhere"


def test_references_resolve_and_a_reload_does_not_stack_them(
    mertina_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERTINA_T_PATH", "/base")
    _env(
        mertina_home,
        "MERTINA_T_PATH=/extra:${MERTINA_T_PATH}\nMERTINA_T_D=${MERTINA_T_NONE:-dflt}\n",
    )

    load_mertina_dotenv()
    load_mertina_dotenv()

    assert os.environ["MERTINA_T_PATH"] == "/extra:/base"
    assert os.environ["MERTINA_T_D"] == "dflt"


def test_a_value_changed_since_the_last_load_is_the_new_baseline(
    mertina_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERTINA_T_PATH", "/base")
    _env(mertina_home, "MERTINA_T_PATH=/extra:${MERTINA_T_PATH}\n")
    load_mertina_dotenv()

    os.environ["MERTINA_T_PATH"] = "/changed"
    load_mertina_dotenv()

    assert os.environ["MERTINA_T_PATH"] == "/extra:/changed"


def test_a_utf8_bom_does_not_stick_to_the_first_name(mertina_home: Path) -> None:
    _env(mertina_home, codecs.BOM_UTF8 + b"MERTINA_T_A=1\n")

    load_mertina_dotenv()

    assert os.environ["MERTINA_T_A"] == "1"


def test_a_utf16_file_is_rewritten_as_utf8(mertina_home: Path) -> None:
    path = _env(mertina_home, "MERTINA_T_A=wide\r\n".encode("utf-16"))

    load_mertina_dotenv()

    assert os.environ["MERTINA_T_A"] == "wide"
    assert path.read_text(encoding="utf-8") == "MERTINA_T_A=wide\n"  # UTF-8 now, no BOM


def test_nul_bytes_are_stripped(mertina_home: Path) -> None:
    path = _env(mertina_home, b"MERTINA_T_A=a\x00b\n")

    load_mertina_dotenv()

    assert os.environ["MERTINA_T_A"] == "ab"
    assert path.read_text(encoding="utf-8") == "MERTINA_T_A=ab\n"


def test_a_utf32_file_is_refused_and_left_untouched(
    mertina_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    raw = "MERTINA_T_A=1\n".encode("utf-32")
    path = _env(mertina_home, raw)

    # Upstream will not rewrite it, and the NUL-padded names it then parses cannot go into
    # os.environ.
    with pytest.raises(ValueError, match="embedded null"):
        load_mertina_dotenv()

    assert path.read_bytes() == raw
    assert "UTF-32 BOM detected" in caplog.text


def test_non_ascii_is_stripped_from_credentials_only(
    mertina_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cyrillic_a = chr(0x430)
    _env(mertina_home, f"MERTINA_T_API_KEY=sk-{cyrillic_a}bc\nMERTINA_T_NAME=café\n")

    load_mertina_dotenv()
    load_mertina_dotenv()

    assert os.environ["MERTINA_T_API_KEY"] == "sk-bc"
    assert os.environ["MERTINA_T_NAME"] == "café"
    err = capsys.readouterr().err
    assert err.count("Warning: MERTINA_T_API_KEY contained 1 non-ASCII character") == 1
    assert "U+0430" in err
    assert "into the .env file (edit it in a plain-text editor)" in err


def test_config_references_see_the_loaded_dotenv(mertina_home: Path) -> None:
    _env(mertina_home, "MERTINA_T_KEY=sk-dotenv\n")
    (mertina_home / "config.yaml").write_text(
        "model:\n  api_key: ${MERTINA_T_KEY}\n", encoding="utf-8"
    )

    load_mertina_dotenv()

    assert load_config()["model"]["api_key"] == "sk-dotenv"


def test_sanitize_env_lines_normalizes_assignments_only() -> None:
    lines = ["  A=1  \r\n", "# keep  \n", "\n", "B=x C=y"]

    assert _sanitize_env_lines(lines) == ["A=1\n", "# keep  \n", "\n", "B=x C=y\n"]
