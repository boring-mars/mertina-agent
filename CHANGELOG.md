# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-10-07

The third cut of the v0.1 agent core: the first built-in tool, `web_search`, and a config layer
that points the agent at a real endpoint.

### Added

- `web_search`, the first built-in tool, behind the `WebSearchProvider` interface and the provider
  registry. The one provider is the keyless DuckDuckGo plugin (ddgs): each search runs in a
  disposable child process with a 30-second deadline, stops when the turn is stopped, and gets an
  environment with provider keys stripped. A backend chosen in config.yaml that is not registered
  is an error rather than a silent switch to another provider
- Discovery of bundled plugins, gated by `plugins.enabled` and `plugins.disabled` in config.yaml
- The config layer: `config.yaml` and `.env` in the Mertina home (`%LOCALAPPDATA%\mertina` on
  Windows, `~/.mertina` elsewhere, or `MERTINA_HOME`). Defaults are merged in, `${VAR}`
  references are expanded, and a broken file falls back to the defaults with a warning
- `run_agent.main()` loads the `.env` into the process environment, then takes the model endpoint
  from its arguments, config.yaml `model` (a model name, or `default`, `base_url` and `api_key`),
  and `OPENAI_BASE_URL` / `OPENAI_API_KEY`, in that order
- `scripts/real_loop.py`, the acceptance run against a real endpoint: a `web_search` round trip,
  and a stop the next turn recovers from
- Dependencies: `pyyaml`, `ddgs` and `python-dotenv`

### Changed

- The `agent` section of config.yaml sets the retry count and the tool-use guidance again, and
  `timezone` sets the time zone when `MERTINA_TIMEZONE` is unset

## [0.1.1] - 2026-10-05

The second cut of the v0.1 agent core: streaming, retry and interruption, still against a fake
model.

### Added

- Streaming: text deltas reach `stream_delta_callback` with reasoning tags scrubbed across chunk
  boundaries, tool names reach `tool_gen_callback`, and `tool_progress_callback` reports each tool
  call as it starts and finishes. Dropped streams are reopened before any text is shown, and a
  drop after text keeps what was shown
- Retry: rate limits, server errors and connection errors are retried with jittered backoff that
  honours `Retry-After`; other client errors end the turn with copy that says why
- Interruption: `interrupt()` stops the turn while the model is thinking, streaming or backing off,
  and reaches running tools through a per-thread interrupt bit. The request is aborted at once,
  unfinished tool calls get results, and a half-streamed reply is kept, so the next turn continues
  on a valid history
- The layered system prompt: the default identity, tool-use guidance, and the caller's system
  message with a timestamp line (`MERTINA_TIMEZONE` sets the zone)

### Changed

- Every model request runs on a worker thread, so a stop takes effect without waiting for the
  provider to answer

## [0.1.0] - 2026-09-30

The first cut of the v0.1 agent core, copied from Hermes Agent and cut down.
The full loop runs against a fake model; real endpoints arrive in v0.1.2.

### Added

- `AIAgent` with its `chat()` and `run_conversation()` entry points
- The conversation loop and its turn phases: model call, tool call dispatch, appending results,
  repeating until the model answers
- The tool executor, running independent tool calls in parallel
- The tool registry and `model_tools`
- The transport interface and a Chat Completions transport for OpenAI-compatible endpoints,
  with client construction and lifecycle
- A per-turn iteration budget and a basic stop flag

[Unreleased]: https://github.com/boring-mars/mertina-agent/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/boring-mars/mertina-agent/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/boring-mars/mertina-agent/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/boring-mars/mertina-agent/releases/tag/v0.1.0
