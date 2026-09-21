# Roadmap

This roadmap describes how Mertina Agent grows from a minimal cloud harness into a cloud-and-local
personal assistant. It covers direction and order, not dates.

Mertina is built by copying code from [Hermes Agent](https://github.com/NousResearch/hermes-agent)
and cutting it down. Each milestone brings over the Hermes modules that provide its capability,
keeps their structure, and removes whatever the milestone does not need. The Hermes paths listed
under each milestone are the starting points. We do not try to stay in sync with upstream after
copying.

## How features are prioritized

Every Hermes feature was reviewed and given one of these priorities:

| Priority | Meaning |
|---|---|
| **P0** | The minimal, complete, testable harness. Everything on this roadmap's milestones |
| **P1** | What turns the harness into an assistant you can deploy to the cloud and use every day |
| **P2** | Local clients, more channels, multimedia, and scale. Planned, not scheduled |
| **P3** | Features that only matter for coding. Considered last |
| **Won't do** | Out of scope for Mertina |

Only P0 is broken into milestones below. P1 and later are summarized at the end and will be
scheduled once P0 is done.

## Overview

| Milestone | Theme | Status |
|---|---|---|
| [v0.1](#v01--agent-core) | Agent core: a tool-using agent loop you can run and test | Planned |
| [v0.2](#v02--gateway-and-sessions) | Gateway and sessions: the agent as a long-running service with an HTTP API | Planned |
| [v0.3](#v03--web-ui) | Web UI: chat with the agent in the browser | Planned |
| [v0.4](#v04--memory-and-skills) | Memory and skills: the assistant remembers you and follows reusable procedures | Planned |
| [v0.5](#v05--scheduled-tasks) | Scheduled tasks: the assistant runs work on a schedule | Planned |

When v0.5 is done, the P0 harness is complete.

Versions follow [Semantic Versioning](docs/en/development/versioning-and-release.md). Until 1.0,
minor versions may contain breaking changes.

## Guiding principles

1. **Minimal, complete, testable.** P0 builds the smallest harness that covers the whole path, from
   a message in the browser to the model, tools, memory, and back. Every milestone is testable on its own.
2. **Copy, then cut.** Start from working Hermes code rather than rewriting it. Remove what the current
   milestone does not need, and bring it back only when a later milestone does.
3. **Keep abstractions, not implementations.** Where Hermes has an interface with many implementations
   (model transports, search providers), keep the interface and implement one.
4. **One process.** The gateway process serves the API and the web UI, runs agents, and runs the cron
   ticker, following Hermes's gateway model.
5. **Keep the agent core reusable.** The agent loop and tools do not depend on the gateway, so future
   local clients can reuse them.
6. **Cloud first, local extends.** The cloud service is always the complete product. Local clients
   come later and add to it.

---

## v0.1 — Agent core

**Goal:** run a tool-using agent from Python. It calls an OpenAI-compatible model, searches the web
when it needs to, streams its answer, and can be stopped.

### Scope

- **Agent loop.** Copy `AIAgent` and its turn loop, then strip them down to: model call, tool call
  dispatch, append results, repeat until the model answers.
- **Two entry points.** `chat()` returns the final text; `run_conversation()` returns messages,
  metadata, and usage.
- **Model transport.** Keep the transport abstraction (message and tool conversion, request building,
  response normalization) and implement only Chat Completions against any OpenAI-compatible endpoint.
- **Streaming.** Text deltas and tool progress are emitted as they arrive.
- **Tool execution.** The tool registry, parallel execution of independent tool calls, and an
  iteration budget per turn.
- **Minimal retry.** Exponential backoff on rate limits and server errors. Full error classification
  and fallback come in P1.
- **Minimal interrupt.** A stop flag checked before each model call and while streaming. On stop,
  pending tool calls are closed with an "interrupted" result so the history stays valid. Hermes's
  interrupt handling during retries and its `/steer` integration are left out.
- **System prompt.** The layered prompt builder, with only the layers this milestone needs
  (identity, tool guidance, time). Memory and skills layers arrive in v0.4.
- **One tool.** `web_search`, behind the search provider interface, with one provider implemented.
  It exists to exercise the tool path end to end.
- **Configuration.** A config file plus environment variables for the model endpoint, API key,
  model name, and search provider.

### Hermes references

| Mertina | Hermes |
|---|---|
| Agent loop | `run_agent.py` (`AIAgent`), `agent/conversation_loop.py`; since the Sep 2026 decomposition the turn phases live in `agent/turn_*.py` |
| Transports | `agent/transports/base.py`, `agent/transports/chat_completions.py` |
| Tool registry and execution | `tools/registry.py`, `model_tools.py`, `agent/tool_executor.py`, `agent/iteration_budget.py` |
| Retry and interrupt | `agent/retry_utils.py`, `tools/interrupt.py` |
| Prompt assembly | `agent/prompt_builder.py`, `agent/system_prompt.py` |
| Web search | `tools/web_tools.py`, `agent/web_search_provider.py`, `plugins/web/` |
| Configuration | `hermes_cli/config.py` |

### Done when

- A script can run a conversation in which the model calls `web_search` and answers from the results
- Unit tests cover the loop with a fake model client: plain answers, tool calls, parallel tool calls,
  the iteration budget, retries, and a stop in the middle of a turn
- After a stop, the conversation can continue without the model API rejecting the history

---

## v0.2 — Gateway and sessions

**Goal:** the agent runs inside a long-running gateway process, conversations are stored, and
everything can be driven over HTTP.

### Scope

- **Gateway process.** Copy the gateway runner and keep only what hosts the API server and runs
  agent turns. Platform adapters, profile multiplexing, kanban, relay, and scale-to-zero are left out.
- **Session management.** Each conversation maps to a session, and turns within one session run one
  at a time.
- **Session storage.** Copy SessionDB (SQLite) and cut it down to sessions and messages.
- **Session recovery.** Resume a session by ID with its full history.
- **Sessions API.** Create, list, read, rename, and delete sessions, and read their messages.
- **Runs API.** Start a run, stream its events over SSE, and stop it. A client that reconnects
  can reattach to a run that is still going.
- **Local only.** In P0 the gateway listens on localhost with no authentication. Authentication
  comes before any cloud deployment, in P1.

### Hermes references

| Mertina | Hermes |
|---|---|
| Gateway process | `gateway/run.py` (`GatewayRunner`), `gateway/config.py` |
| Session management | `gateway/session.py`, `gateway/turn_lease.py` |
| Session storage | `hermes_state.py`, `hermes_state_schema.py` |
| Session recovery | `hermes_cli/session_recovery.py` |
| Sessions and Runs API | `gateway/platforms/api_server.py` |

### Done when

- Using only HTTP, you can create a session, start a run, stream its events, stop it, and read the
  stored messages afterwards
- After a gateway restart, earlier sessions are still listed and can be resumed
- API tests cover the Sessions and Runs endpoints

---

## v0.3 — Web UI

**Goal:** chat with the agent in the browser, with streaming answers and a list of past conversations.

### Scope

- **Dashboard shell.** Copy Hermes's `web/` layout, theme, UI components, and API client. The built
  UI is served by the gateway process.
- **Chat page.** Rewrite the chat page as a native chat view on top of the Runs API: streaming
  Markdown, collapsible tool calls, and a stop button. Hermes's embedded terminal chat is left out.
- **Sessions page.** List sessions, open one to continue it, rename it, and delete it.

### Hermes references

| Mertina | Hermes |
|---|---|
| Dashboard shell | `web/src/App.tsx`, `web/src/components/`, `web/src/lib/api.ts` |
| Chat page | `web/src/pages/ChatPage.tsx` (rewritten) |
| Sessions page | `web/src/pages/SessionsPage.tsx` |

### Done when

- From a fresh checkout, one command builds the UI and starts the gateway, and you can chat in the browser
- Answers and tool calls stream in; the stop button ends a run
- Reloading the page during a run reattaches to it

---

## v0.4 — Memory and skills

**Goal:** the assistant remembers what you tell it across sessions and can follow reusable procedures.

### Scope

- **Memory tool.** Two bounded stores: notes the agent keeps, and a profile of the user. The model
  reads and updates them through a `memory` tool.
- **Memory injection.** A snapshot of both stores is added to the system prompt when a session starts.
- **Skills.** Skills in the `SKILL.md` format are loaded from a skills directory with progressive
  disclosure: the prompt lists names and descriptions, and the model loads a skill's full content
  when it needs it. No skills are bundled; a few are written for testing.

### Hermes references

| Mertina | Hermes |
|---|---|
| Memory | `tools/memory_tool.py`, `agent/memory_manager.py` |
| Prompt layers | `agent/prompt_builder.py` |
| Skills | `tools/skills_tool.py`, `agent/skill_utils.py` |

### Done when

- A fact stated in one session is used in a new session
- The model loads a matching skill and follows it, and ignores skills that do not apply
- Tests cover memory capacity limits, prompt injection of the snapshots, and skill loading

---

## v0.5 — Scheduled tasks

**Goal:** the assistant does work on a schedule, even when no browser is open.

### Scope

- **Cron scheduler.** The cron ticker runs inside the gateway process and executes due jobs as agent runs.
- **`cronjob` tool.** The model creates, lists, pauses, and removes jobs during a conversation.
- **Results.** Each run is stored as a session, so its output can be read in the web UI.

### Hermes references

| Mertina | Hermes |
|---|---|
| Scheduler | `cron/scheduler.py`, `cron/jobs.py`, `gateway/run.py` (`_start_cron_ticker`) |
| Tool | `tools/cronjob_tools.py` |

### Done when

- Asking the assistant to "search for X every morning at 8" creates a job, and the job runs on schedule
- Job results show up as sessions in the web UI
- Tests cover schedule parsing and job execution with a fake clock

---

## After P0

P1 is scheduled once the P0 harness is complete. It covers:

- **Robustness:** full error handling and fallback providers, tool-loop guardrails, context
  compression, recovery after crashes
- **Everyday use:** assistant persona (SOUL.md), reasoning controls, message queueing, clarifying
  questions, a task list, undo and retry, slash commands, image input, more web UI pages
- **Learning loop:** memory safety scanning, background review after each turn, session search,
  skills the agent writes itself
- **Execution:** web page extraction, file and terminal tools, code execution, all inside a Docker
  sandbox with command approval and write safety
- **Automation:** job lifecycle and delivery, subagent delegation, time zones
- **Cloud deployment:** Docker deployment, authentication for the UI and API, logs, health checks,
  backups, usage tracking
- **First messaging channels:** Feishu (Lark) and Telegram
- **Extensibility:** plugins, hooks, and MCP servers

P2 then brings local clients (CLI, TUI, desktop) with session sync, more messaging platforms,
multimedia, multiple users, and scale.

## How this roadmap changes

The roadmap is a living document. To propose a change, such as a new capability, a different order,
or a different priority, open a feature request or a discussion. Changes to milestone scope are made
by pull request to this file, so the history of decisions stays in git.
