"""First-run setup: choose a model endpoint and a session database, then save them.

``mertina setup`` asks a few questions in the terminal and writes the answers to
an env file: ``--env-file`` when given, otherwise the file ``mertina`` already
reads, otherwise ``~/.mertina/.env``. A bare ``mertina`` runs it automatically
when no settings exist and the terminal is interactive.

Every answer is checked before it is saved. The API key is checked by listing
the endpoint's models and sending one tiny request with the chosen model; the
database by connecting and creating (or validating) the session tables. Secrets
are read without echo and never printed, and the file is written with mode 0600.

Prompts run synchronously; only each check runs in its own event loop, so
Ctrl+C or Ctrl+D at any prompt cancels setup at once and nothing is written.
"""

import asyncio
import getpass
import os
import re
import socket
import tempfile
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO
from urllib.parse import quote

from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from mertina_agent.agent.model_client import ModelClient
from mertina_agent.config import Settings
from mertina_agent.database.runtime import open_database
from mertina_agent.exceptions import (
    ConfigurationError,
    DatabaseError,
    ModelRequestError,
    ModelResponseError,
    StorageError,
)
from mertina_agent.state.schema import Schema

BASE_URL = "MERTINA_LLM_BASE_URL"
API_KEY = "MERTINA_LLM_API_KEY"
MODEL = "MERTINA_LLM_MODEL"
DATABASE_URL = "MERTINA_DATABASE_URL"
SETTINGS_KEYS = (BASE_URL, API_KEY, MODEL, DATABASE_URL)
"""The settings this wizard writes; any other line in the file is kept as it is."""

PROBE_TIMEOUT_S = 20.0
"""Per-request timeout for the model checks, shorter than a real turn's."""

SHOWN_MODELS = 30
"""Most models listed by number; any other name can still be typed."""

FILE_HEADER = (
    "# Mertina settings written by `mertina setup`. Environment variables override\n"
    "# these values; .env.example in the repository lists every other setting.\n"
)

_SAFE_VALUE = re.compile(r"[A-Za-z0-9_./:@%+=,~-]*\Z")
_ASSIGNMENT = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


@dataclass(frozen=True)
class Provider:
    """A model endpoint preset; ``base_url`` is ``None`` when the user types it."""

    name: str
    base_url: str | None


PROVIDERS = (
    Provider("DeepSeek (official API)", "https://api.deepseek.com"),
    Provider("OpenAI", "https://api.openai.com/v1"),
    Provider("Another OpenAI-compatible endpoint", None),
)


@dataclass(frozen=True)
class Backend:
    """A database choice; ``default_port`` is ``None`` for the local SQLite file."""

    name: str
    scheme: str
    default_port: int | None


BACKENDS = (
    Backend("SQLite file on this machine (nothing to install)", "sqlite+aiosqlite", None),
    Backend("PostgreSQL (needs an existing database and account)", "postgresql+asyncpg", 5432),
    Backend("MySQL (needs an existing database and account)", "mysql+asyncmy", 3306),
)


class SetupCancelledError(Exception):
    """The user pressed Ctrl+C or Ctrl+D; nothing is written."""


class CheckFailedError(Exception):
    """A model or database check failed; the message is safe to show."""


@dataclass
class Terminal:
    """Questions and answers on a text terminal; the readers are injectable for tests."""

    out: TextIO
    read_line: Callable[[str], str] = input
    read_secret: Callable[[str], str] = field(default=getpass.getpass)

    def say(self, text: str = "") -> None:
        """Print one line."""
        self.out.write(text + "\n")
        self.out.flush()

    def _read(self, reader: Callable[[str], str], prompt: str) -> str:
        try:
            return reader(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise SetupCancelledError from None

    def ask(self, prompt: str, default: str | None = None) -> str:
        """Ask for text; an empty answer returns ``default`` (or ``""``)."""
        suffix = f" [{default}]" if default else ""
        return self._read(self.read_line, f"{prompt}{suffix}: ") or (default or "")

    def secret(self, prompt: str) -> str:
        """Ask for a secret without echoing it."""
        return self._read(self.read_secret, f"{prompt}: ")

    def choose(self, prompt: str, options: Sequence[str], default: int = 0) -> int:
        """List numbered options and return the chosen index."""
        for number, option in enumerate(options, start=1):
            self.say(f"  [{number}] {option}")
        while True:
            answer = self.ask(prompt, str(default + 1))
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.say(f"  Enter a number from 1 to {len(options)}.")

    def confirm(self, prompt: str, *, default: bool = True) -> bool:
        """Ask a yes/no question."""
        while True:
            answer = self._read(self.read_line, f"{prompt} [{'Y/n' if default else 'y/N'}]: ")
            if not answer:
                return default
            if answer.lower() in {"y", "yes", "n", "no"}:
                return answer.lower().startswith("y")
            self.say("  Answer y or n.")


class ModelProbe(Protocol):
    """Checks a model endpoint; :class:`EndpointProbe` calls the real one."""

    async def list_models(self, settings: Settings) -> list[str]:
        """Return the endpoint's model IDs."""
        ...

    async def try_model(self, settings: Settings) -> None:
        """Send one tiny request with ``settings.llm_model``."""
        ...


class EndpointProbe:
    """Check the configured endpoint through the project's own model client."""

    async def list_models(self, settings: Settings) -> list[str]:
        """Return the endpoint's model IDs."""
        async with ModelClient(settings) as client:
            return await client.list_models()

    async def try_model(self, settings: Settings) -> None:
        """Send one tiny request with ``settings.llm_model``."""
        async with ModelClient(settings) as client:
            await client.complete([{"role": "user", "content": "Reply with the single word OK."}])


def default_env_path(home: Path | None = None) -> Path:
    """The user-level settings file, ``~/.mertina/.env``."""
    return (home if home is not None else Path.home()) / ".mertina" / ".env"


def read_env(path: Path) -> dict[str, str]:
    """Read ``KEY=value`` pairs from ``path``; a missing file reads as empty."""
    if not path.is_file():
        return {}
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def _format_value(value: str) -> str:
    if _SAFE_VALUE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_env(path: Path, values: Mapping[str, str]) -> None:
    """Set ``values`` in ``path``, keeping every other line, with mode 0600.

    Each key replaces its first assignment and drops later duplicates; keys not
    yet present are appended. The file is replaced atomically, so a failure
    leaves the previous version intact.
    """
    if any(char in value for value in values.values() for char in "\r\n\0"):
        message = "Setting values must be single-line"
        raise ValueError(message)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if not lines:
        lines = FILE_HEADER.splitlines()
    pending = dict(values)
    output: list[str] = []
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) in values:
            key = match.group(1)
            if key in pending:
                output.append(f"{key}={_format_value(pending.pop(key))}")
            continue
        output.append(line)
    output.extend(f"{key}={_format_value(value)}" for key, value in pending.items())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
        Path(temporary).chmod(0o600)
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def server_url(
    scheme: str, *, host: str, port: int, database: str, user: str, password: str
) -> str:
    """Build a database URL, percent-encoding every part a user typed.

    SQLAlchemy's own renderer leaves spaces unescaped, which the settings
    validator rejects, so each component is quoted here instead.
    """
    netloc_host = f"[{host}]" if ":" in host else host
    credentials = quote(user, safe="") + (f":{quote(password, safe='')}" if password else "")
    return f"{scheme}://{credentials}@{netloc_host}:{port}/{quote(database, safe='')}"


def describe_model_error(exc: Exception) -> str:
    """A short, safe explanation of a failed model check."""
    if isinstance(exc, ModelRequestError):
        if exc.kind == "timeout":
            return "the endpoint did not answer in time"
        if exc.kind == "connection":
            return "could not connect; check the URL, your network and any proxy"
        reasons = {
            401: "the API key was rejected",
            402: "the account has no balance left",
            403: "access was denied; the key may lack permission or your IP may not be allowed",
            404: "not found; check the base URL and the model name",
            429: "rate limited; wait a moment and try again",
        }
        reason = reasons.get(exc.status_code or 0, "the endpoint returned an error")
        return f"{reason} (HTTP {exc.status_code})"
    if isinstance(exc, ModelResponseError):
        return "the endpoint did not answer like an OpenAI-compatible API"
    return str(exc)


def _causes(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        orig = getattr(current, "orig", None)
        pending.extend(
            [
                current.__context__,
                current.__cause__,
                orig if isinstance(orig, BaseException) else None,
            ]
        )


def describe_database_error(exc: DatabaseError) -> str:
    """Explain a failed connection from the driver's error codes, never its message.

    Driver messages can contain the URL or password, so only typed codes are read:
    asyncpg's ``sqlstate``, MySQL's numeric error code and socket errors.
    """
    for cause in _causes(exc):
        sqlstate = getattr(cause, "sqlstate", None)
        code = cause.args[0] if cause.args and isinstance(cause.args[0], int) else None
        if sqlstate in {"28P01", "28000"} or code == 1045:
            return "the user name or password was rejected"
        if sqlstate == "3D000" or code == 1049:
            return "that database does not exist; create it first"
        if code == 1044:
            # MySQL reports a missing database this way to accounts that cannot see it.
            return "that database does not exist, or the account is not allowed to use it"
        if sqlstate == "42501" or code == 1142:
            return "the account is not allowed to use that database"
        if code == 2003 or isinstance(cause, ConnectionRefusedError):
            return "nothing answered at that host and port; is the server running?"
        if isinstance(cause, socket.gaierror):
            return "the host name could not be resolved"
    if exc.kind == "timeout":
        return "the server did not answer in time"
    return f"the database could not be used ({exc.kind})"


def _validated(build: Callable[[], Settings]) -> Settings:
    try:
        return build()
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        message = f"invalid value for {fields}"
        raise CheckFailedError(message) from None


def _model_settings(base_url: str, key: str, model: str = "setup-probe") -> Settings:
    return _validated(
        lambda: Settings(
            _env_file=None,
            llm_base_url=base_url,
            llm_api_key=SecretStr(key) if key else None,
            llm_model=model,
            llm_timeout_s=PROBE_TIMEOUT_S,
        )
    )


def _database_settings(url: str) -> Settings:
    return _validated(lambda: Settings(_env_file=None, database_url=SecretStr(url)))


def _check[T](check: Coroutine[object, object, T]) -> T:
    """Run one async check in its own event loop and translate its failure."""
    try:
        return asyncio.run(check)
    except KeyboardInterrupt:
        raise SetupCancelledError from None
    except (ModelRequestError, ModelResponseError, ConfigurationError) as exc:
        raise CheckFailedError(describe_model_error(exc)) from None
    except DatabaseError as exc:
        raise CheckFailedError(describe_database_error(exc)) from None


class Wizard:
    """The two setup steps; each returns the settings it decided on."""

    def __init__(self, term: Terminal, probe: ModelProbe, current: Mapping[str, str]) -> None:
        """Ask on ``term``, check with ``probe`` and offer ``current`` values as defaults."""
        self.term = term
        self.probe = probe
        self.current = current

    def _after_failure(self, problem: str, retry: str, *, restart: bool = True) -> str:
        """Report a failed check; return ``retry``, ``restart`` or ``keep``."""
        self.term.say(f"  ✗ {problem}")
        actions = ("retry", "restart", "keep") if restart else ("retry", "keep")
        labels = {
            "retry": retry,
            "restart": "Start this step over",
            "keep": "Save without checking",
        }
        return actions[self.term.choose("What next", [labels[action] for action in actions])]

    # Step 1 ----------------------------------------------------------------
    def model(self) -> dict[str, str]:
        """Choose the provider, key and model, checking each against the endpoint."""
        self.term.say("\nStep 1 of 2 · Model")
        while True:
            chosen = self._model_once()
            if chosen is not None:
                return chosen

    def _model_once(self) -> dict[str, str] | None:
        saved_url = self.current.get(BASE_URL)
        default = next(
            (
                index
                for index, item in enumerate(PROVIDERS)
                if saved_url and item.base_url == saved_url
            ),
            len(PROVIDERS) - 1 if saved_url else 0,
        )
        provider = PROVIDERS[
            self.term.choose("Provider", [item.name for item in PROVIDERS], default)
        ]
        base_url = provider.base_url or self.term.ask("Base URL", saved_url)
        try:
            _model_settings(base_url, "")
        except CheckFailedError:
            self.term.say("  ✗ Enter an http(s) URL without credentials, query or fragment.")
            return None
        saved_key = self.current.get(API_KEY) if base_url == saved_url else None
        models: list[str] = []
        while True:
            hint = " (Enter keeps the saved key)" if saved_key else ""
            key = self.term.secret(f"API key{hint}") or saved_key or ""
            if not key and provider.base_url is not None:
                self.term.say("  This provider needs an API key.")
                continue
            self.term.say("  Checking the key…")
            try:
                models = _check(self.probe.list_models(_model_settings(base_url, key)))
            except CheckFailedError as exc:
                if "HTTP 404" in str(exc):
                    self.term.say("  The endpoint does not list models; type the model name.")
                    break
                action = self._after_failure(str(exc), "Enter the key again")
                if action == "retry":
                    continue
                if action == "restart":
                    return None
                return self._finish_model(base_url, key, check=False, models=[])
            self.term.say(f"  ✓ Key accepted, {len(models)} models available")
            break
        return self._finish_model(base_url, key, check=True, models=models)

    def _pick_model(self, models: Sequence[str]) -> str:
        saved = self.current.get(MODEL)
        if not models:
            while not (name := self.term.ask("Model name", saved)):
                self.term.say("  A model name is required.")
            return name
        shown = list(models[:SHOWN_MODELS])
        for number, name in enumerate(shown, start=1):
            self.term.say(f"  [{number}] {name}")
        if len(models) > len(shown):
            self.term.say(f"  … and {len(models) - len(shown)} more; type any name")
        default = saved if saved in models else shown[0]
        answer = self.term.ask("Model (number or name)", default)
        if answer.isdigit() and 1 <= int(answer) <= len(shown):
            return shown[int(answer) - 1]
        return answer

    def _finish_model(
        self, base_url: str, key: str, *, check: bool, models: Sequence[str]
    ) -> dict[str, str] | None:
        while True:
            model = self._pick_model(models)
            chosen = {BASE_URL: base_url, API_KEY: key, MODEL: model}
            if not check:
                self.term.say("  ! Saved without checking the model")
                return chosen
            self.term.say(f"  Sending a test request to {model}…")
            try:
                _check(self.probe.try_model(_model_settings(base_url, key, model)))
            except CheckFailedError as exc:
                action = self._after_failure(str(exc), "Choose another model")
                if action == "retry":
                    continue
                if action == "restart":
                    return None
                return chosen
            self.term.say(f"  ✓ {model} answered")
            return chosen

    # Step 2 ----------------------------------------------------------------
    def database(self, home: Path) -> str:
        """Choose where conversations are saved, then connect and prepare the tables."""
        self.term.say("\nStep 2 of 2 · Where conversations are saved")
        saved = self.current.get(DATABASE_URL)
        while True:
            try:
                parsed = make_url(saved) if saved else None
            except ArgumentError:
                parsed = None
            schemes = [backend.scheme for backend in BACKENDS]
            default = (
                schemes.index(parsed.drivername) if parsed and parsed.drivername in schemes else 0
            )
            names = [item.name for item in BACKENDS]
            backend = BACKENDS[self.term.choose("Database", names, default)]
            previous = (
                parsed if parsed is not None and parsed.drivername == backend.scheme else None
            )
            if backend.default_port is None:
                fallback = str(default_env_path(home).parent / "mertina.sqlite3")
                path = self.term.ask("SQLite file", (previous and previous.database) or fallback)
                file = Path(path).expanduser().resolve()
                # The folder holds every conversation; keep it private when we create it.
                file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                url = f"sqlite+aiosqlite:///{file}"
            else:
                url = self._server_url(backend, previous)
            try:
                self._prepare(url, server=backend.default_port is not None)
            except CheckFailedError as exc:
                saved = url
                retry = "Try again (your answers are kept as defaults)"
                if self._after_failure(str(exc), retry, restart=False) == "keep":
                    return url
                continue
            return url

    def _server_url(self, backend: Backend, previous: URL | None) -> str:
        """Ask for the server fields, offering the previous URL's values as defaults."""
        host = self.term.ask("Host", (previous and previous.host) or "127.0.0.1")
        port = (previous and previous.port) or backend.default_port
        while True:
            answer = self.term.ask("Port", str(port))
            if answer.isdigit() and 0 < int(answer) < 65536:
                break
            self.term.say("  Enter a port from 1 to 65535.")
        name = self.term.ask("Database name", (previous and previous.database) or "mertina")
        user = self.term.ask("User", (previous and previous.username) or "mertina")
        saved_password = previous.password if previous is not None else None
        hint = " (Enter keeps the saved password)" if saved_password else ""
        password = self.term.secret(f"Password{hint}") or str(saved_password or "")
        return server_url(
            backend.scheme, host=host, port=int(answer), database=name, user=user, password=password
        )

    def _prepare(self, url: str, *, server: bool) -> None:
        settings = _database_settings(url)
        self.term.say("  Connecting…")
        backend, present, expected = _check(_inspect_tables(settings))
        if not present:
            if server and not self.term.confirm(
                "  This database has no Mertina tables yet. Create them now?"
            ):
                message = "the session tables were not created"
                raise CheckFailedError(message)
            _check(_initialize(settings, bootstrap=True))
            self.term.say(f"  ✓ {backend}: created the session tables")
            return
        if present != expected:
            message = "this database has an incomplete set of Mertina tables; use an empty one"
            raise CheckFailedError(message)
        try:
            count = _check(_initialize(settings, bootstrap=False))
        except StorageError:
            message = "this database has a Mertina schema this version cannot use"
            raise CheckFailedError(message) from None
        self.term.say(f"  ✓ {backend}: session tables ready, {count} saved conversations")


async def _inspect_tables(settings: Settings) -> tuple[str, set[str], set[str]]:
    schema = Schema()
    async with await open_database(settings) as database:
        async with database.transaction(write=False) as connection:
            names = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
        expected = set(schema.metadata.tables)
        return database.backend, names & expected, expected


async def _initialize(settings: Settings, *, bootstrap: bool) -> int:
    schema = Schema()
    async with await open_database(settings) as database:
        await schema.initialize(database, bootstrap=bootstrap)
        async with database.transaction(write=False) as connection:
            query = select(func.count()).select_from(schema.sessions)
            return int((await connection.execute(query)).scalar_one())


def run_setup(
    path: Path,
    term: Terminal,
    *,
    probe: ModelProbe | None = None,
    home: Path | None = None,
) -> bool:
    """Ask, check and save the model and database settings to ``path``.

    Args:
        path: The env file to update; created (with its directory) if missing.
        term: Where to ask the questions.
        probe: Model checks; the real endpoint by default.
        home: Home directory override for tests.

    Returns:
        ``True`` when the settings were saved, ``False`` when setup was cancelled.
    """
    current = read_env(path)
    term.say(
        "Mertina setup: pick a model and where to save conversations.\n"
        f"Settings file: {path}\n"
        "Press Enter to accept the value in [brackets]; Ctrl+C cancels without saving."
    )
    wizard = Wizard(term, probe if probe is not None else EndpointProbe(), current)
    try:
        values = wizard.model()
        values[DATABASE_URL] = wizard.database(home if home is not None else Path.home())
    except SetupCancelledError:
        term.say("\nSetup cancelled; nothing was saved.")
        return False
    write_env(path, values)
    term.say(f"\nSaved to {path} (readable only by you).")
    overridden = [key for key in SETTINGS_KEYS if key in os.environ]
    if overridden:
        term.say(f"Note: {', '.join(overridden)} set in the environment still override the file.")
    return True
