"""The .env tokenizer."""

import codecs
from pathlib import Path

from mertina.agent.secret_scope import invalidate_env_file_cache, load_env_file


def test_tokenizes_the_dotenv_subset(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        "export EXPORTED=yes\n"
        "SPACED = padded  \n"
        "INLINE=value # trailing comment\n"
        "HASH=foo#bar\n"
        "SINGLE='a # b'\n"
        'DOUBLE="say \\"hi\\" \\\\ ok" # note\n'
        "NOEQUALS\n"
        "=novalue\n",
        encoding="utf-8",
    )

    assert load_env_file(env) == {
        "PLAIN": "value",
        "EXPORTED": "yes",
        "SPACED": "padded",
        "INLINE": "value",
        "HASH": "foo#bar",
        "SINGLE": "a # b",
        "DOUBLE": 'say "hi" \\ ok',
    }


def test_a_bom_and_latin_1_are_tolerated(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(codecs.BOM_UTF8 + b"FIRST=1\nNAME=caf\xe9\n")

    assert load_env_file(env) == {"FIRST": "1", "NAME": "café"}


def test_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / ".env") == {}


def test_results_are_fresh_dicts_and_follow_rewrites(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    first = load_env_file(env)
    first["A"] = "mutated"

    assert load_env_file(env) == {"A": "1"}

    env.write_text("A=22\n", encoding="utf-8")
    invalidate_env_file_cache(env)

    assert load_env_file(env) == {"A": "22"}
