# Testing

**English** | [中文](../../zh/development/testing.md)

Tests let contributors change code with confidence. CI runs the whole suite on every PR, and a PR can't be merged while it is red.

## Running tests

```bash
uv run pytest                          # all tests
uv run pytest tests/storage            # one directory
uv run pytest -k session               # tests whose name matches "session"
uv run pytest -x --lf                  # stop at first failure, rerun last failures
uv run pytest --cov=mertina            # with coverage report
```

## Layout

```
tests/
├── conftest.py          # shared fixtures
├── unit/                # fast, isolated, no network or external services
│   └── storage/
│       └── test_session_store.py
└── integration/         # talk to real services (database, object storage, ...)
    └── ...
```

- The structure of `tests/unit/` mirrors `mertina/`
- Files are named `test_<module>.py`, and functions `test_<behavior>`, for example
  `test_load_session_raises_when_missing`

## Kinds of tests

| Kind | Scope | External services | Runs |
|---|---|---|---|
| **Unit** | A function or class | None: use fakes or mocks | Every PR, must be fast (whole suite in seconds) |
| **Integration** | Several components with real infrastructure | Real, started locally (for example with Docker Compose) | Every PR in CI, marked `@pytest.mark.integration` |
| **End-to-end** | The deployed service through its public API | A test deployment | Before releases |

Integration tests can be skipped locally with `uv run pytest -m "not integration"`.

## What must be tested

- **Every bug fix comes with a regression test** that fails without the fix
- **Every new feature** has tests for the main path and the important failure paths
- Public APIs and configuration parsing are tested thoroughly, because breaking them affects users
- Security-relevant logic (authentication, authorization, tenant isolation, input validation) is tested,
  including the negative cases: the request that must be rejected

## Coverage

- Coverage is measured in CI and shown on PRs
- There is no hard percentage target, because a number encourages meaningless tests
- A PR should not noticeably lower coverage without a reason. Reviewers can ask for missing tests

## Writing good tests

- **Test behavior, not implementation.** Assert on outputs and observable effects, not on private methods or call counts
- **One behavior per test.** A failing test should tell you what broke from its name alone
- **Arrange, act, assert.** Keep the three parts visible
- **Deterministic.** No dependence on the current time, random numbers, test order or real network.
  Inject clocks and random seeds, and use fixtures
- **Fast.** A slow unit test is usually a sign of hidden I/O
- **Independent.** Each test sets up and cleans up its own state. Use `tmp_path` for files
- Use `pytest.mark.parametrize` instead of copy-pasting tests with different inputs
- Mock at the boundary of the system (HTTP clients, cloud SDKs, LLM APIs), not inside your own code

## LLM and external APIs

The project calls LLM providers and cloud services. In tests:

- Unit and integration tests never call real LLM or paid APIs. Use fakes or recorded responses
- Never use real API keys in tests. CI has no production credentials
- If a recorded response fixture is used, strip any keys, tokens or personal data from it before committing

## Flaky tests

A test that sometimes fails is a bug. If you find one:

1. Open an issue labeled `flaky-test`
2. If it blocks other people's PRs, mark it `@pytest.mark.skip(reason="flaky, see #123")` in a separate PR
3. Fix it and remove the skip. Don't leave skipped tests lying around
