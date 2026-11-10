# Mertina Agent

**A personal AI assistant that lives in the cloud and extends to your devices.**

> **Status: pre-alpha.** The v0.1 agent core is available: an asynchronous, tool-using agent loop
> that streams its answer, runs `web_search` calls in parallel, retries transient model errors and
> can be stopped mid-turn. The v0.2 gateway adds a local HTTP API for sessions and runs, streams
> run events over SSE, and stores conversations in SQLite, PostgreSQL or MySQL so they survive a
> restart. The web UI is not implemented yet.
> See the [Roadmap](ROADMAP.md) for what is being built and in what order.

Mertina Agent is a general-purpose personal assistant. It runs as an always-on cloud service
that you reach from a web page. Local clients (desktop app, CLI/TUI) will follow. They run the agent
on your own machine and sync with the cloud.

It is a slimmed-down rebuild of [Hermes Agent](https://github.com/NousResearch/hermes-agent) by
Nous Research. Hermes is a powerful agent with a very large surface area: many providers,
messaging platforms, terminal backends, and research tooling. Mertina copies Hermes's core pieces
(the agent loop, the gateway and its API server, and the web UI shell), cuts them down to a minimal
harness, and then adds capabilities one at a time, following the direction Hermes has taken.

## Product direction

**Cloud first.** The cloud service is the product's home. It is always on, so the assistant can
remember you, run scheduled work while you are away, and be reached from any browser.

**Local extends cloud.** Local clients come later. They run the same agent on your machine and sync
with the cloud, starting with sessions. Over time they add capabilities that only make sense locally,
such as working with local files, local apps, and local models.

**Small core, grown on purpose.** Every Hermes feature has been given a priority. The first phase
(P0) builds the minimal, complete, testable harness; everything else waits for its turn. Keeping the
core small and readable is a goal in its own right.

## What P0 delivers

- An agent loop that calls any OpenAI-compatible model, runs tools in parallel, streams its output,
  retries on transient errors, and can be stopped
- One tool, `web_search`, behind a pluggable search provider interface
- A gateway process with an HTTP API for sessions and runs, with run events streamed over SSE
- Resumable conversations with a configurable SQLite, PostgreSQL, or MySQL backend
- A web UI for chatting and browsing past sessions
- Memory across sessions, and skills the assistant loads when a task calls for them
- Scheduled tasks the assistant creates for you in conversation

P0 runs locally for a single user. Authentication, sandboxed code execution, messaging channels, and
cloud deployment come in P1. See the [Roadmap](ROADMAP.md) for milestones and priorities.

## Architecture (P0)

```
 Browser: web UI (React + Vite, shell from Hermes's web/, native chat page)
        │  HTTP: REST for sessions, SSE for streaming run events
        ▼
 Gateway process (aiohttp, from Hermes's gateway/)
  ├── API server     /api/sessions/*, /v1/runs (+ SSE events, stop)
  ├── Static files   the built web UI
  ├── Cron ticker    runs scheduled jobs as agent runs
  └── Agent runtime  turn loop: model call → tool calls → repeat
        ├── Model transport  Chat Completions, any OpenAI-compatible endpoint
        ├── Tools            web_search, memory, skills, cronjob
        └── Prompt builder   identity, memory snapshot, skills index, time
        │
        ▼
 SessionDB (SQLite / PostgreSQL / MySQL, adapted from Hermes's hermes_state)
```

Key decisions:

| Area | Decision |
|---|---|
| Base | Copy and cut down Hermes code, keeping its structure. We do not try to stay in sync with upstream |
| Language | Python for the agent and the gateway, TypeScript for the web UI |
| Server | One gateway process based on Hermes's aiohttp API server. It serves the API and the web UI, runs agents, and runs the cron ticker |
| Web UI | Hermes's `web/` shell (layout, theme, components). The chat page is rewritten as a native chat view on top of the Runs API |
| Models | Keep Hermes's transport abstraction, implement Chat Completions only |
| Storage | Adapt Hermes's SessionDB for SQLite, PostgreSQL, and MySQL behind one storage contract; one database per process, chosen at startup |
| Users | Single user in P0. Multiple users come later |

## Planned repository layout

```
mertina-agent/
├── src/mertina_agent/
│   ├── agent/        # agent loop, transports, prompt builder
│   ├── tools/        # tool registry and built-in tools
│   ├── plugins/      # tool providers, such as DuckDuckGo search
│   ├── database/     # SQLite, PostgreSQL and MySQL connection adapters
│   ├── state/        # session storage: schema, messages, turn commits
│   └── gateway/      # gateway process, API server, runs and SSE, cron ticker
├── web/              # React + Vite frontend
├── dev/databases/    # local PostgreSQL and MySQL containers for development
├── tests/
├── docs/
└── ROADMAP.md
```

## Getting started

Use Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). Install the project and development
tools from the lockfile:

```bash
uv sync --locked
```

Copy `.env.example` to `.env`, then explicitly set `MERTINA_LLM_BASE_URL` and `MERTINA_LLM_MODEL`
to an endpoint and model you intend to use. Set `MERTINA_LLM_API_KEY` only if that endpoint requires
authentication. An empty key selects unauthenticated mode; the client does not fall back to
`OPENAI_API_KEY`. Never commit `.env` or share its secrets. Environment variables override `.env`.

### Run an agent turn

Run one real agent turn explicitly, from the repository root:

```bash
uv run python examples/agent_chat.py --env-file .env
```

The model is offered `web_search` (DuckDuckGo through the `ddgs` package, no key needed) and a
demonstration clock tool. Streamed text and tool progress go to stderr; a JSON summary with the
final answer, exit reason, request count, tools used and token usage goes to stdout. Pass your own
question as an argument. Model requests may incur charges on a hosted endpoint, and web searches
are sent to DuckDuckGo.

To see a turn stopped while a tool runs and then continued from its history:

```bash
uv run python examples/stop_and_resume.py --env-file .env
```

Both examples require an existing env file with an explicit endpoint and model, so an empty file
cannot silently select a hosted service through library defaults.

### Use the agent from Python

```python
import asyncio

from mertina_agent.agent.core import Agent
from mertina_agent.config import load_settings


async def main() -> None:
    settings = load_settings()
    async with Agent(settings, event_callback=print) as agent:
        first = await agent.run_conversation("What is new in Python this month?")
        print(first["final_response"])
        second = await agent.run_conversation(
            "Summarize that in one line.", conversation_history=first["messages"]
        )
        print(second["final_response"])


asyncio.run(main())
```

`run_conversation()` returns a result with `final_response`, the complete `messages`, `api_calls`,
`completed` / `failed` / `interrupted` / `partial` flags, `turn_exit_reason` and summed `usage`.
The caller owns the conversation: pass `messages` back as `conversation_history` to continue it.
`chat()` returns only the final text. `agent.interrupt()` stops the running turn from any task or
thread; the returned history stays valid for the next turn. Progress arrives as typed events
(`TextDelta`, `ToolGenerationStarted`, `ToolCallStarted`, `ToolCallFinished`, `RetryScheduled`,
and one of `RunCompleted`, `RunStopped` or `RunFailed`) through `event_callback`.

### Current scope

- One OpenAI-compatible Chat Completions endpoint, streaming by default (`MERTINA_LLM_STREAM`).
- ReAct turns: model call, tool calls, results, repeat, up to `MERTINA_MAX_ITERATIONS` model calls,
  after which the model is asked once, without tools, for a summary.
- Independent `web_search` calls run concurrently; results always follow the model's call order.
- Timeouts, connection errors, HTTP 408/429/5xx and malformed responses are retried with jittered
  backoff or the provider's `Retry-After`, up to `MERTINA_LLM_MAX_ATTEMPTS` attempts per call.
- A stop request is honoured before each model call, while a call is in flight, during backoff and
  before each tool. Unanswered tool calls are closed so the history can be sent again.
- Truncated tool calls are never executed; a truncated text answer is returned as partial.

`MERTINA_LLM_TIMEOUT_S` controls network-operation timeouts, not a total turn deadline. The SDK
itself never retries or follows redirects. The lower-level model client remains available on its
own; see `examples/model_call.py`:

```bash
uv run python examples/model_call.py --env-file .env
```

### Run the gateway

The gateway is a single process that keeps sessions in a database, runs agent turns in the
background and serves them over HTTP. It listens on a loopback address only and has no
authentication in P0, so do not expose it to a network.

**1. Choose a database.** Without `MERTINA_DATABASE_URL`, sessions go to SQLite at
`~/.mertina/mertina.sqlite3`. To use another file, or PostgreSQL or MySQL, set the URL in `.env`:

```bash
MERTINA_DATABASE_URL=sqlite+aiosqlite:////absolute/path/mertina.sqlite3
MERTINA_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@127.0.0.1:55432/DATABASE
MERTINA_DATABASE_URL=mysql+asyncmy://USER:PASSWORD@127.0.0.1:53306/DATABASE
```

For local PostgreSQL and MySQL containers, see [dev/databases](dev/databases/README.md). A
connection failure stops the gateway; it never falls back to another database.

**2. Create the schema once.** The gateway refuses to start until the session tables exist.
`init` only creates them in an empty database and is safe to run again; `check` validates them
without changing anything:

```bash
uv run python -m mertina_agent.state init --env-file .env
uv run python -m mertina_agent.state check --env-file .env
```

**3. Start the gateway.** It listens on `http://127.0.0.1:8765` by default
(`MERTINA_GATEWAY_HOST`, `MERTINA_GATEWAY_PORT`). Stop it with Ctrl+C or SIGTERM: it stops
accepting runs, asks running turns to stop, saves what they produced and then exits.

```bash
uv run python -m mertina_agent.gateway --env-file .env
```

**4. Talk to it over HTTP.** In another terminal, create a session, start a run and stream its
events:

```bash
curl -s -X POST http://127.0.0.1:8765/api/sessions \
  -H 'Content-Type: application/json' -d '{"title": "First chat"}'
```

The response contains the session `id`. Start a run in that session; it returns `202` with a
`run_id` straight away and the turn continues in the background:

```bash
curl -s -X POST http://127.0.0.1:8765/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "SESSION_ID", "message": "What is new in Python this month?"}'
```

Stream the run's events as Server-Sent Events:

```bash
curl -N http://127.0.0.1:8765/v1/runs/RUN_ID/events
```

```text
id: 1
event: run.accepted
data: {"id":1,"run_id":"…","type":"run.accepted","data":{"status":"pending"}}

id: 2
event: run.started
data: {"id":2,"run_id":"…","type":"run.started","data":{"status":"running"}}

id: 3
event: run.committing
data: {"id":3,"run_id":"…","type":"run.committing","data":{"status":"committing"}}

id: 4
event: run.completed
data: {"id":4,"run_id":"…","type":"run.completed","data":{"status":"completed","durable":true,"revision":1,"error":null,"reconciliation_required":false}}
```

Between `run.started` and the final event, a streaming model also sends `text.delta`, tool calls
send `tool.generating`, `tool.started` and `tool.finished`, and a retried model call sends
`retry.scheduled`, after which the client should discard the text it has shown for that attempt.
The final event is `run.completed`, `run.partial`, `run.stopped` or `run.failed`. It is sent only
after the turn has been saved, and `durable: true` confirms that.

Read the saved conversation:

```bash
curl -s http://127.0.0.1:8765/api/sessions/SESSION_ID/messages
```

The API:

| Method and path | What it does |
|---|---|
| `POST /api/sessions` | Create a session. Body: `{"title": "…"}` (optional, `{}` uses a default title) |
| `GET /api/sessions` | List sessions, oldest first. Query: `limit` (1–100), and both `after_created_at_us` and `after_id` from the previous page's `next_cursor` |
| `GET /api/sessions/{id}` | Read a session. `active_run_id` names the run in progress, if any |
| `PATCH /api/sessions/{id}` | Rename. Body: `{"title": "…", "expected_version": N}`, where `N` is the session's current `metadata_version` |
| `DELETE /api/sessions/{id}` | Delete a session and its messages |
| `GET /api/sessions/{id}/messages` | Read messages in order. Query: `limit`, `after` (last `seq` seen) and `through` (the `through` of the first page, so later pages stay consistent) |
| `POST /v1/runs` | Start a turn. Body: `{"session_id": "…", "message": "…"}` |
| `GET /v1/runs/{run_id}` | Read a run's status and the text produced so far |
| `GET /v1/runs/{run_id}/events` | Stream events. Send `Last-Event-ID` to resume after a disconnect |
| `POST /v1/runs/{run_id}/stop` | Ask a running turn to stop. Repeating it, or stopping a finished run, is harmless |

Behaviour to know about:

- **One turn per session at a time.** Starting a run in a session that already has one returns
  `409`; so do renaming with an outdated `expected_version` and deleting a session with a run in
  progress. Different sessions run concurrently, up to `MERTINA_GATEWAY_MAX_ACTIVE_RUNS`; beyond that the
  gateway answers `503`.
- **Disconnecting does not stop a run.** Only the stop endpoint does. Reconnect with
  `Last-Event-ID` to receive the events you missed. If they are no longer buffered, the gateway
  answers `409`; read `GET /v1/runs/{run_id}` and the session's messages instead.
- **Restarts keep saved turns, not running ones.** After a restart, sessions and their messages
  are still there and the next run continues from them. A turn that was still running when the
  process was killed is lost, and run IDs and their events are kept in memory only, for
  `MERTINA_GATEWAY_RUN_TTL_S` seconds after a run ends.
- **Browsers on other sites are rejected.** A request whose `Origin` header does not match the
  gateway's own address gets `403`.
- **Errors** have the form `{"error": {"code": "…", "message": "…"}}` and never include
  connection strings, SQL, prompts or stack traces.

Two scripts drive the same flow from Python. With the gateway running:

```bash
uv run python examples/gateway_chat.py "Hello"
uv run python examples/gateway_reconnect.py "Hello"
```

`gateway_chat.py` creates a session (or continues one with `--session-id`), streams a run and
prints the saved messages; `--stop-after SECONDS` stops the run part way through.
`gateway_reconnect.py` disconnects from the event stream after the first event and reattaches to
the same run. Both print progress to stderr and a JSON result to stdout.

### Offline checks and real-provider acceptance

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv run pre-commit run --all-files
```

These checks never contact a model service. `uv run pytest` also runs the integration tests: one
searches DuckDuckGo, and the database, session-storage and gateway tests run against SQLite in a
temporary directory. To run them against PostgreSQL and MySQL as well, start the
[development containers](dev/databases/README.md) and set `MERTINA_TEST_POSTGRESQL_URL` and
`MERTINA_TEST_MYSQL_URL`; without them those backends are skipped. Skip all integration tests with
`uv run pytest -m "not integration"` (the pre-commit hook does). The examples are not collected by
pytest. Passing offline checks does not establish
real-provider acceptance; that is recorded separately after explicitly running the examples.

See the [contributing guide](docs/en/development/contributing.md) for the development workflow and
the [agent-core phase plan](docs/plans/v0.1-phase-3-agent-loop.md) for acceptance criteria.

## Documentation

- [Roadmap](ROADMAP.md): priorities and milestones
- [Agent-core phase plan](docs/plans/v0.1-phase-3-agent-loop.md) and its
  [development log](docs/plans/v0.1-phase-3-development-log.md): checkpoints and acceptance evidence
- [Model-transport phase plan](docs/plans/v0.1-phase-2-model-transport.md): scope and acceptance criteria
- v0.2 plans: [gateway and sessions](docs/plans/v0.2-gateway-sessions.md),
  [database access](docs/plans/v0.2-phase-1-databases.md) and the
  [development log](docs/plans/v0.2-development-log.md)
- Architecture decisions: [supported databases](docs/adr/0001-database-backends.md) and
  [asynchronous relational storage](docs/adr/0002-async-relational-storage.md)
- Hermes provenance: [agent core](docs/sources/hermes-agent-core.md),
  [model transport](docs/sources/hermes-model-transport.md) and
  [gateway and sessions](docs/sources/hermes-gateway-sessions.md), with fixed source versions,
  adaptations, and the third-party license
- [Development guidelines](docs/en/development/README.md): branching, commits, pull requests,
  code review, coding style, testing, releases, security
- [开发规范（中文）](docs/zh/development/README.md)

## Contributing

Contributions are welcome. Read the [contributing guide](CONTRIBUTING.md) and the
[code of conduct](CODE_OF_CONDUCT.md) first. To report a security issue, follow the
[security policy](SECURITY.md) and do not open a public issue.

## Acknowledgements

Mertina Agent is built on ideas and code from [Hermes Agent](https://github.com/NousResearch/hermes-agent)
by [Nous Research](https://nousresearch.com), released under the MIT License. Where Mertina copies
code from Hermes, the original copyright notice is kept, as the license requires. Source snapshots
and deliberate changes are recorded in the provenance documents for the
[agent core](docs/sources/hermes-agent-core.md), the [model transport](docs/sources/hermes-model-transport.md)
and the [gateway and sessions](docs/sources/hermes-gateway-sessions.md).
Its original license is included in [LICENSES/Hermes-Agent-MIT.txt](LICENSES/Hermes-Agent-MIT.txt).

## License

[MIT](LICENSE)
