# Mertina Agent

**A personal AI assistant that lives in the cloud and extends to your devices.**

> **Status: pre-alpha.** The v0.1 Python agent loop can run locally; the gateway and web UI are still planned.
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

The v0.1 loop needs Python 3.11+, the `openai` package, and an OpenAI-compatible model endpoint.
Copy `mertina.example.toml` to `mertina.toml`, set `OPENAI_API_KEY` in your environment, then run:

```sh
python -m pip install openai
python run_agent.py "Say hello" --config mertina.toml
```

You can also set `MERTINA_BASE_URL` and `MERTINA_MODEL` instead of using a config file.
CLI flags override the environment and config values. The [contributing guide](docs/en/development/contributing.md)
describes the broader development workflow.

`web_search` is offered to the model only when `BRAVE_SEARCH_API_KEY` is set or a search provider
is injected into `AIAgent`. Without one, ordinary model conversations still work. Set
`MERTINA_SEARCH_PROVIDER=none` to disable search explicitly.

## Documentation

- [Roadmap](ROADMAP.md): priorities and milestones
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
code from Hermes, the original copyright notice is kept, as the license requires.

## License

[MIT](LICENSE)
