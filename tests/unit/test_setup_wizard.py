"""First-run setup: env file editing, URL building, error wording and the full flow."""

import asyncio
import io
import socket
import stat

import pytest
from pydantic import SecretStr
from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.engine import make_url

from mertina_agent.config import load_settings
from mertina_agent.database.config import validate_database_url
from mertina_agent.database.runtime import open_database
from mertina_agent.exceptions import DatabaseError, ModelRequestError
from mertina_agent.setup_wizard import (
    API_KEY,
    BASE_URL,
    DATABASE_URL,
    MODEL,
    Terminal,
    describe_database_error,
    describe_model_error,
    read_env,
    run_setup,
    server_url,
    write_env,
)
from mertina_agent.state.schema import Schema

SECRET = "sk-test-secret-value"


def script(*answers):
    """A reader that returns ``answers`` in order, then behaves like Ctrl+D."""
    pending = list(answers)
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        if not pending:
            raise EOFError
        return pending.pop(0)

    read.prompts = prompts
    return read


class FakeProbe:
    """Model checks that record what they were asked and fail on demand."""

    def __init__(self, models=("model-b", "model-a"), list_errors=(), try_errors=()):
        self.models = sorted(models)
        self.list_errors = list(list_errors)
        self.try_errors = list(try_errors)
        self.calls = []

    async def list_models(self, settings):
        key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
        self.calls.append(("list", settings.llm_base_url, key))
        if self.list_errors:
            raise self.list_errors.pop(0)
        return self.models

    async def try_model(self, settings):
        key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
        self.calls.append(("try", settings.llm_base_url, key, settings.llm_model))
        if self.try_errors:
            raise self.try_errors.pop(0)


def http_error(status):
    return ModelRequestError(f"HTTP {status}", kind="http", status_code=status)


def setup(tmp_path, lines, secrets, probe=None):
    out = io.StringIO()

    def echoed(reader):
        # Like input() and getpass(): the prompt reaches the terminal, the answer does not.
        def read(prompt):
            out.write(prompt)
            return reader(prompt)

        return read

    term = Terminal(out, read_line=echoed(script(*lines)), read_secret=echoed(script(*secrets)))
    path = tmp_path / "home" / ".mertina" / ".env"
    saved = run_setup(path, term, probe=probe or FakeProbe(), home=tmp_path / "home")
    return saved, path, out.getvalue()


# Env file -------------------------------------------------------------------


def test_write_env_keeps_other_lines_and_replaces_only_its_keys(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# my notes\nMERTINA_LOG_LEVEL=DEBUG\nexport MERTINA_LLM_MODEL=old\n"
        "# MERTINA_LLM_API_KEY=commented\nMERTINA_LLM_MODEL=duplicate\n"
    )
    write_env(path, {MODEL: "new", API_KEY: 'a b"c\\d'})

    text = path.read_text()
    assert text.startswith("# my notes\nMERTINA_LOG_LEVEL=DEBUG\nMERTINA_LLM_MODEL=new\n")
    assert "# MERTINA_LLM_API_KEY=commented" in text
    assert "duplicate" not in text
    assert read_env(path) == {
        "MERTINA_LOG_LEVEL": "DEBUG",
        MODEL: "new",
        API_KEY: 'a b"c\\d',
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [item.name for item in tmp_path.iterdir()] == [".env"]


def test_write_env_creates_a_private_file_with_a_header(tmp_path):
    path = tmp_path / "new" / ".env"
    write_env(path, {MODEL: "m"})
    assert path.read_text().startswith("# Mertina settings written by `mertina setup`")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_settings(env_file=path).llm_model == "m"


def test_write_env_rejects_multiline_values(tmp_path):
    with pytest.raises(ValueError, match="single-line"):
        write_env(tmp_path / ".env", {API_KEY: "a\nMERTINA_LLM_MODEL=x"})
    assert not (tmp_path / ".env").exists()


# URLs and messages ----------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "db.example", "::1"])
def test_server_url_round_trips_awkward_credentials(host):
    url = server_url(
        "postgresql+asyncpg",
        host=host,
        port=5432,
        database="my db",
        user="u@x",
        password="p@ss:w/rd ?#%+简",
    )
    validate_database_url(url)
    parsed = make_url(url)
    assert (parsed.username, parsed.password, parsed.database) == (
        "u@x",
        "p@ss:w/rd ?#%+简",
        "my db",
    )
    assert (parsed.host, parsed.port) == (host, 5432)


class DriverError(Exception):
    def __init__(self, *args, sqlstate=None):
        super().__init__(*args)
        self.sqlstate = sqlstate


def _database_error(cause, kind="connection"):
    try:
        try:
            raise cause
        except BaseException as inner:
            raise DatabaseError(kind) from inner
    except DatabaseError as exc:
        return exc


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (DriverError("password=hunter2 rejected", sqlstate="28P01"), "password was rejected"),
        (DriverError(1045, "Access denied for hunter2"), "password was rejected"),
        (DriverError(1049, "Unknown database"), "does not exist"),
        (DriverError("x", sqlstate="3D000"), "does not exist"),
        (DriverError(1044, "Access denied"), "does not exist, or the account"),
        (ConnectionRefusedError(61, "refused"), "nothing answered"),
        (socket.gaierror(8, "nodename"), "could not be resolved"),
    ],
)
def test_database_errors_are_explained_from_codes_never_messages(cause, expected):
    message = describe_database_error(_database_error(cause))
    assert expected in message
    assert "hunter2" not in message


def test_database_timeout_and_unknown_failures_have_generic_wording():
    assert "in time" in describe_database_error(DatabaseError("timeout"))
    assert "(query)" in describe_database_error(DatabaseError("query"))


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "key was rejected"), (402, "no balance"), (403, "IP"), (500, "HTTP 500")],
)
def test_model_errors_name_the_likely_cause(status, expected):
    assert expected in describe_model_error(http_error(status))


# Full flow ------------------------------------------------------------------


def _schema_ready(path):
    async def check():
        async with await open_database(load_settings(env_file=path)) as database:
            await Schema().initialize(database, bootstrap=False)

    asyncio.run(check())


def test_defaults_choose_deepseek_and_a_local_sqlite_file(tmp_path):
    probe = FakeProbe()
    saved, path, output = setup(tmp_path, ["", "", "", ""], [SECRET], probe)

    assert saved
    values = read_env(path)
    assert values[BASE_URL] == "https://api.deepseek.com"
    assert values[API_KEY] == SECRET
    assert values[MODEL] == "model-a"
    database = tmp_path / "home" / ".mertina" / "mertina.sqlite3"
    assert values[DATABASE_URL] == f"sqlite+aiosqlite:///{database.resolve()}"
    _schema_ready(path)
    assert probe.calls == [
        ("list", "https://api.deepseek.com", SECRET),
        ("try", "https://api.deepseek.com", SECRET, "model-a"),
    ]
    assert "created the session tables" in output
    assert SECRET not in output
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700


def test_running_again_offers_saved_values_and_keeps_the_key(tmp_path):
    setup(tmp_path, ["", "", "", ""], [SECRET])
    probe = FakeProbe()
    saved, path, output = setup(tmp_path, ["", "2", "", ""], [""], probe)

    assert saved
    assert read_env(path)[API_KEY] == SECRET
    assert read_env(path)[MODEL] == "model-b"
    assert probe.calls[0] == ("list", "https://api.deepseek.com", SECRET)
    assert "Enter keeps the saved key" in output
    assert "session tables ready, 0 saved conversations" in output


def test_a_rejected_key_can_be_entered_again(tmp_path):
    probe = FakeProbe(list_errors=[http_error(401)])
    saved, path, output = setup(tmp_path, ["", "1", "", "", ""], ["wrong", SECRET], probe)

    assert saved
    assert read_env(path)[API_KEY] == SECRET
    assert "the API key was rejected (HTTP 401)" in output
    assert "wrong" not in output


def test_an_endpoint_without_a_model_list_asks_for_the_name(tmp_path):
    probe = FakeProbe(list_errors=[http_error(404)])
    lines = ["3", "http://127.0.0.1:11434/v1", "local-model", "", ""]
    saved, path, _ = setup(tmp_path, lines, [""], probe)

    assert saved
    assert read_env(path)[BASE_URL] == "http://127.0.0.1:11434/v1"
    assert read_env(path)[MODEL] == "local-model"
    assert probe.calls[-1] == ("try", "http://127.0.0.1:11434/v1", "", "local-model")


def test_settings_can_be_saved_without_checking(tmp_path):
    error = ModelRequestError("offline", kind="connection")
    saved, path, output = setup(
        tmp_path, ["", "3", "typed-model", "", ""], [SECRET], FakeProbe(list_errors=[error])
    )

    assert saved
    assert read_env(path)[MODEL] == "typed-model"
    assert "could not connect" in output
    assert "Saved without checking" in output


def test_cancelling_writes_nothing(tmp_path):
    saved, path, output = setup(tmp_path, ["", ""], [SECRET])

    assert not saved
    assert not path.exists()
    assert "nothing was saved" in output


def test_an_unreachable_server_is_explained_and_can_be_kept(tmp_path):
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    lines = ["", "", "2", "127.0.0.1", str(port), "mertina", "mertina", "2"]
    saved, path, output = setup(tmp_path, lines, [SECRET, "db-secret"])

    assert saved
    url = make_url(read_env(path)[DATABASE_URL])
    assert (url.drivername, url.port, url.password) == ("postgresql+asyncpg", port, "db-secret")
    assert "nothing answered at that host and port" in output
    assert "db-secret" not in output


def test_a_database_with_some_mertina_tables_is_refused(tmp_path):
    database = tmp_path / "partial.sqlite3"

    async def create_partial():
        settings = load_settings(env_file=None).model_copy(
            update={"database_url": SecretStr(f"sqlite+aiosqlite:///{database}")}
        )
        async with await open_database(settings) as handle:
            metadata = MetaData()
            Table("mertina_sessions", metadata, Column("id", Integer, primary_key=True))
            async with handle.transaction() as connection:
                await connection.run_sync(metadata.create_all)

    asyncio.run(create_partial())
    lines = ["", "", "", str(database)]
    saved, _, output = setup(tmp_path, lines, [SECRET])

    assert not saved
    assert "incomplete set of Mertina tables" in output
