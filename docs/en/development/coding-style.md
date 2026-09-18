# Coding Style

**English** | [中文](../../zh/development/coding-style.md)

The goal is code that any contributor can read and change safely. Tools enforce most of the rules below,
so run them before pushing rather than memorizing the rules.

## Tooling

| Tool | Purpose | Command |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Python version, virtual env, dependencies, build | `uv sync`, `uv add`, `uv build` |
| [Ruff](https://docs.astral.sh/ruff/) | Linting and formatting | `uv run ruff check .`, `uv run ruff format .` |
| [mypy](https://mypy.readthedocs.io/) | Static type checking | `uv run mypy src` |
| [pytest](https://docs.pytest.org/) | Tests | `uv run pytest` |
| [pre-commit](https://pre-commit.com/) | Runs the checks before each commit | `uv run pre-commit install` |

All tool configuration lives in `pyproject.toml`. Don't add separate `setup.py`, `setup.cfg`, `requirements.txt`
or tool-specific config files unless a tool can't be configured any other way.

## Project layout

```
mertina-agent/
├── pyproject.toml        # project metadata, dependencies, tool config
├── uv.lock               # locked dependency versions (committed)
├── src/
│   └── mertina_agent/    # the package
│       ├── __init__.py
│       └── ...
├── tests/                # tests, mirroring the src layout
├── docs/                 # documentation
└── .github/              # CI workflows, templates, CODEOWNERS
```

- The package uses the **src layout**, so tests run against the installed package, not stray local files
- The package name is `mertina_agent`. The distribution (PyPI) name is `mertina-agent`

## Python version

- The minimum supported Python version is set in `requires-python` in `pyproject.toml`
- Use modern syntax available in that version: `list[str]` instead of `List[str]`, `X | None` instead of `Optional[X]`
- CI tests both the minimum and the latest supported version

## Formatting

- Formatting is whatever `ruff format` produces. Don't fight it
- Line length: 100
- Double quotes for strings
- Imports are sorted by Ruff: standard library, third party, then first party

## Naming

| Kind | Style | Example |
|---|---|---|
| Modules, packages | `snake_case`, short | `session_store.py` |
| Functions, variables | `snake_case` | `load_session()` |
| Classes, type aliases | `PascalCase` | `SessionStore` |
| Constants | `UPPER_SNAKE_CASE` | `DEFAULT_TIMEOUT_S` |
| Private | Leading underscore | `_parse_header()` |

- Prefer clear names over comments that explain unclear names
- Include units in names where they matter: `timeout_s`, `max_size_bytes`
- Avoid abbreviations, except well-known ones (`id`, `url`, `http`, `db`)

## Type hints

- All public functions, methods and class attributes have type hints
- mypy must pass. Don't add `# type: ignore` without an error code and a reason:
  `# type: ignore[import-untyped]  # library ships no stubs`
- Prefer precise types (`Literal`, `TypedDict`, dataclasses, Pydantic models) over `dict[str, Any]`
- Avoid `Any` in public interfaces

## Docstrings and comments

- Public modules, classes and functions have docstrings in **Google style**
- The docstring says what the function does and any non-obvious behavior (errors raised, side effects).
  It doesn't repeat the type hints
- Comments explain *why*, not *what*. Delete commented-out code. Git remembers it
- Code, comments, docstrings and log messages are written in English

```python
def load_session(session_id: str, *, timeout_s: float = 5.0) -> Session:
    """Load a session from the configured store.

    Raises:
        SessionNotFoundError: If no session with this ID exists.
        StorageTimeoutError: If the store does not respond within ``timeout_s``.
    """
```

## Errors

- Raise specific exceptions. Define project exceptions in one place, deriving from a common base class
- Never use a bare `except:`. Catch `Exception` only at the outermost layer (request handlers, workers), and log it there
- Don't swallow exceptions silently. If you catch one, handle it, re-raise it, or log it with context
- Chain exceptions when re-raising: `raise StorageError("...") from exc`

## Logging

- Use the standard `logging` module with a module-level logger: `logger = logging.getLogger(__name__)`
- Never use `print()` in library or service code
- Choose the level carefully: `DEBUG` for diagnostics, `INFO` for significant events, `WARNING` for recoverable problems,
  `ERROR` for failures that need attention
- **Never log secrets, tokens, full prompts containing user data, or personal data**
- Use lazy formatting (`logger.info("loaded %s", session_id)`) or structured fields, not f-strings, in hot paths

## Configuration

- Configuration comes from **environment variables** (12-factor), with safe defaults for local development
- Read and validate configuration in one place at startup (for example with `pydantic-settings`), and fail fast on invalid values
- Environment variables use the `MERTINA_` prefix: `MERTINA_LOG_LEVEL`, `MERTINA_DATABASE_URL`
- Every setting is documented, and `.env.example` lists all of them with placeholder values

## Async and I/O

- Don't mix blocking I/O into async code. Use async clients or run blocking calls in a thread pool
- Every network call has a timeout
- Retries use exponential backoff and a cap, and only apply to operations that are safe to retry

## Dependencies

- Add with `uv add <package>` (runtime) or `uv add --group dev <package>` (development only), and commit `uv.lock`
- Before adding a dependency, check that it is actively maintained, widely used, and MIT-compatible in license
- Prefer the standard library or an existing dependency over adding a new one for a small feature
- Don't pin exact versions in `pyproject.toml`. Declare a compatible lower bound (`httpx>=0.27`). `uv.lock` pins exact versions
- A PR that adds a dependency explains why in the description
