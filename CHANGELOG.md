# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/boring-mars/mertina-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/boring-mars/mertina-agent/releases/tag/v0.1.0
