"""Configuration must fail before opening a database or exposing credentials."""

import pytest
from pydantic import SecretStr, ValidationError

from mertina_agent.config import Settings, load_settings
from mertina_agent.exceptions import ConfigurationError


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///relative.sqlite3",
        "sqlite+aiosqlite:////absolute/file.sqlite3",
        "sqlite:///:memory:",
        "postgresql://user:secret@localhost:55432/database",
        "postgresql+asyncpg://user:secret@[::1]:5432/database",
        "mysql://user:secret@localhost:53306/database",
        "mysql+asyncmy://user:secret@localhost/database",
    ],
)
def test_supported_database_url_is_preserved_but_hidden(url):
    settings = Settings(database_url=SecretStr(url))
    assert settings.database_url.get_secret_value() == url
    assert url not in repr(settings)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "mongodb://user:secret@localhost/db",
        "postgresql+psycopg://user:secret@localhost/db",
        "sqlite://host/file.db",
        "sqlite:///",
        "mysql://user:secret@localhost:99999/db",
        "postgresql://user:secret@localhost:0/db",
        "mysql://user:secret@localhost/",
        "postgresql://localhost/db",
        "mysql://user:secret@localhost/db?charset=latin1",
        "sqlite:///file.db#fragment",
    ],
)
def test_invalid_database_url_has_safe_error(url, monkeypatch):
    monkeypatch.setenv("MERTINA_DATABASE_URL", url)
    with pytest.raises(ConfigurationError) as error:
        load_settings(env_file=None)
    assert str(error.value) == "Invalid Mertina configuration fields: database_url"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("DATABASE_CONNECT_TIMEOUT_S", "0"),
        ("DATABASE_TIMEOUT_S", "nan"),
        ("DATABASE_POOL_SIZE", "0"),
        ("DATABASE_MAX_OVERFLOW", "-1"),
        ("DATABASE_SQLITE_BUSY_TIMEOUT_MS", "0"),
    ],
)
def test_invalid_database_options_fail_at_startup(field, value, monkeypatch):
    monkeypatch.setenv("MERTINA_" + field, value)
    with pytest.raises(ConfigurationError):
        load_settings(env_file=None)


def test_database_environment_overrides_dotenv(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("MERTINA_DATABASE_URL=sqlite:///file.sqlite3\n", encoding="utf-8")
    monkeypatch.setenv("MERTINA_DATABASE_URL", "sqlite:///override.sqlite3")
    assert (
        load_settings(env_file=path).database_url.get_secret_value() == "sqlite:///override.sqlite3"
    )


def test_null_byte_is_rejected_at_python_boundary():
    with pytest.raises(ValidationError, match="database_url"):
        Settings(database_url=SecretStr("sqlite:///file.db\x00"))
