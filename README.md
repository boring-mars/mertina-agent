# Mertina Agent

**A personal AI assistant that lives in the cloud and extends to your devices.**

> **Status: pre-alpha.** The v0.1 agent core is available: an asynchronous, tool-using agent loop
> that streams its answer, runs `web_search` calls in parallel, retries transient model errors and
> can be stopped mid-turn. The gateway, session storage and web UI are not implemented yet.
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

### Offline checks and real-provider acceptance

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv run pre-commit run --all-files
```

These checks never contact a model service. `uv run pytest` also runs the integration test that
searches DuckDuckGo; skip it offline with `uv run pytest -m "not integration"` (the pre-commit hook
does). The examples are not collected by pytest. Passing offline checks does not establish
real-provider acceptance; that is recorded separately after explicitly running the examples.

See the [contributing guide](docs/en/development/contributing.md) for the development workflow and
the [agent-core phase plan](docs/plans/v0.1-phase-3-agent-loop.md) for acceptance criteria.

## Documentation

- [Roadmap](ROADMAP.md): priorities and milestones
- [Agent-core phase plan](docs/plans/v0.1-phase-3-agent-loop.md) and its
  [development log](docs/plans/v0.1-phase-3-development-log.md): checkpoints and acceptance evidence
- [Model-transport phase plan](docs/plans/v0.1-phase-2-model-transport.md): scope and acceptance criteria
- Hermes provenance: [agent core](docs/sources/hermes-agent-core.md) and
  [model transport](docs/sources/hermes-model-transport.md), with fixed source versions,
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
[agent core](docs/sources/hermes-agent-core.md) and the [model transport](docs/sources/hermes-model-transport.md).
Its original license is included in [LICENSES/Hermes-Agent-MIT.txt](LICENSES/Hermes-Agent-MIT.txt).

## License

[MIT](LICENSE)
