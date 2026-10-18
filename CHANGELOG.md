# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-18

The v0.1 agent core: a tool-using agent loop, copied from Hermes Agent, cut down and
rewritten for asyncio. It runs against any OpenAI-compatible Chat Completions endpoint.

### Added

- `Agent` with its `chat()` and `run_conversation()` entry points; the caller owns the
  history and can pass it straight back into the next turn
- The ReAct conversation loop and its turn phases: model call, tool call validation and
  dispatch, appending results, repeating until the model answers
- An asynchronous model client and a strict Chat Completions transport, with lifecycle
  ownership (`aclose()` and `async with`)
- Streaming: text deltas and tool progress delivered as events (`TextDelta`,
  `ToolGenerationStarted`, `ToolCallStarted`, `ToolCallFinished`), on by default
  (`MERTINA_LLM_STREAM`)
- The tool registry and `model_tools`, with toolsets that can be enabled or disabled per agent
- Parallel execution of independent tool calls, and a per-turn iteration budget
  (`MERTINA_MAX_ITERATIONS`)
- The `web_search` tool, backed by DuckDuckGo through `ddgs` (`MERTINA_WEB_SEARCH_BACKEND`,
  `MERTINA_WEB_SEARCH_TIMEOUT_S`)
- Retry with jittered exponential backoff on 408, 429 and 5xx, honoring `Retry-After`
  (`MERTINA_LLM_MAX_ATTEMPTS`), reported through a `RetryScheduled` event
- Interrupt: `Agent.interrupt()` stops a turn during the model call, while streaming, during
  backoff or while tools run; pending tool calls are closed so the history stays valid and the
  conversation can continue. Failed turns are closed the same way
- A layered system prompt: identity and tool-use guidance, the caller's `system_message`, and
  the current time
- Configuration from `MERTINA_*` environment variables or a `.env` file, validated at startup
- Examples: `agent_chat.py`, `model_call.py` and `stop_and_resume.py`

[Unreleased]: https://github.com/boring-mars/mertina-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/boring-mars/mertina-agent/releases/tag/v0.1.0
