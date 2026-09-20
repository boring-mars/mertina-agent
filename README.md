# Mertina Agent

**A personal AI assistant that lives in the cloud and extends to your devices.**

> **Status: pre-alpha.** A typed, asynchronous model client and an explicit smoke-test example
> are available. The agent loop, tool execution, gateway, and UI are not implemented yet.
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
- Conversations stored in SQLite and resumable
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
 SessionDB (SQLite, from Hermes's hermes_state)
```

Key decisions:

| Area | Decision |
|---|---|
| Base | Copy and cut down Hermes code, keeping its structure. We do not try to stay in sync with upstream |
| Language | Python for the agent and the gateway, TypeScript for the web UI |
| Server | One gateway process based on Hermes's aiohttp API server. It serves the API and the web UI, runs agents, and runs the cron ticker |
| Web UI | Hermes's `web/` shell (layout, theme, components). The chat page is rewritten as a native chat view on top of the Runs API |
| Models | Keep Hermes's transport abstraction, implement Chat Completions only |
| Storage | Hermes's SessionDB on SQLite |
| Users | Single user in P0. Multiple users come later |

## Planned repository layout

```
mertina-agent/
├── src/mertina_agent/
│   ├── agent/        # agent loop, transports, prompt builder
│   ├── tools/        # tool registry and built-in tools
│   ├── gateway/      # gateway process, API server, cron ticker
│   └── state/        # SessionDB (SQLite)
├── web/              # React + Vite frontend
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

Run one real request explicitly, from the repository root:

```bash
uv run python examples/model_call.py --env-file .env
```

This sends the fixed, non-private prompt "Reply with a short greeting." It may incur charges on a
hosted endpoint. The example requires an existing env file and explicitly supplied endpoint/model
settings; an empty file cannot silently select a hosted service through library defaults.
No key is required for an endpoint that supports unauthenticated requests.

To offer a demonstration function tool:

```bash
uv run python examples/model_call.py --env-file .env --tools
```

The example displays normalized text, requested tools, finish reason, refusal, and optional token
usage as JSON. It never executes tools or sends a follow-up request. Offering a tool does not force
the endpoint to call it. Successful exit means a structurally valid response was received; check
`finish_reason` and `refusal` because truncation, filtering, or refusal are not ordinary completion.
Missing usage/counts remain `null`, not fabricated zeroes.

The Python entry point is `mertina_agent.agent.model_client.ModelClient`. It accepts existing
`Settings`, supports `async with`, and exposes `await client.complete(messages, tools=...)`.
Messages support text-only `system`, `developer`, `user`, `assistant`, and `tool` roles; tools use
standard function definitions. An endpoint must support the roles and tool features you request.
Unsupported input fields and malformed responses fail explicitly rather than being silently repaired.

Current scope is one asynchronous, non-streaming Chat Completions request with SDK retries disabled.
There is no conversation loop, streaming, tool execution, automatic backoff, or product-level stop
and history recovery yet. `MERTINA_LLM_TIMEOUT_S` controls network-operation timeouts, not a total
agent-turn deadline. `MERTINA_MAX_ITERATIONS` is reserved for the later agent loop and is not used here.
SDK clients created by the wrapper are closed with it; injected clients remain caller-owned.
Base URLs must not contain embedded credentials, query parameters, fragments, or control characters.
Automatic HTTP redirects are disabled as well as retries. Injected SDKs with conflicting HTTP-level
authentication, cookies, custom headers/query data, or automatic redirects are rejected before dispatch.

### Offline checks and real-provider acceptance

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv run pre-commit run --all-files
```

These checks use offline fixtures or simulated HTTP and do not contact a model service. The example
is not collected by pytest. Passing offline checks does not establish successful real-provider
acceptance; that is recorded separately after explicitly running against a designated endpoint.

See the [contributing guide](docs/en/development/contributing.md) for the development workflow and
the [phase-two plan](docs/plans/v0.1-phase-2-model-transport.md) for acceptance criteria.

## Documentation

- [Roadmap](ROADMAP.md): priorities and milestones
- [Model-transport phase plan](docs/plans/v0.1-phase-2-model-transport.md): scope and acceptance criteria
- [Hermes model-transport provenance](docs/sources/hermes-model-transport.md): fixed source version,
  adaptations, and third-party license
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
code from Hermes, the original copyright notice is kept, as the license requires. The model-transport
source snapshot and deliberate changes are recorded in the [provenance document](docs/sources/hermes-model-transport.md).
Its original license is included in [LICENSES/Hermes-Agent-MIT.txt](LICENSES/Hermes-Agent-MIT.txt).

## License

[MIT](LICENSE)
