# v0.1 Agent Core — Port Design

**English** | [中文](../../zh/design/2026-09-20-agent-core-loop.md)

**Status**: agreed, pending implementation
**Roadmap milestone**: [v0.1 — Agent core](../../../ROADMAP.md#v01--agent-core)
**Branch**: `feat/agent-core-loop`
**Upstream**: [Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT). The survey below is based on a local checkout of upstream; the exact commit SHA is recorded in [Porting from Hermes](../development/porting-from-hermes.md).

---

## 1. Background

The Roadmap's stated principle is "copy, then cut". Before starting, we surveyed the Hermes source and measured it. The conclusion is that the principle has to be applied in layers: it holds for the interface leaf files, and it does not hold for the core loop.

### 1.1 Survey data

Hermes is 6,796 Python files / 1,980,894 lines in total.

Transitive closure from the 14 files named in the Roadmap's v0.1 reference table:

| Measure | Files | Lines |
|---|---:|---:|
| The 14 named files alone | 14 | 15,600 |
| Module-level import closure (must exist to import) | 200 | 105,871 |
| Including function-body imports (needed only on that path) | 1,281 | 568,183 |

The two kinds of import must be counted separately. `plugins/platforms/telegram/adapter.py`, `hermes_cli/kanban_db.py`, `cron/scheduler.py` and `tui_gateway/server.py` appear in the closure only through lazy imports inside function bodies, on code paths this milestone does not need:

```
tools/registry → agent.secret_scope → gateway.config_loader
              → gateway.platforms._shared → tools.send_message_senders → plugins.platforms.telegram.adapter
model_tools    → agent.delegation_context → hermes_cli.kanban_db        (subagents, P1)
tool_executor  → hermes_cli.config → cron.jobs → cron.scheduler          (cron, v0.5)
run_agent      → tools.delegate_tool → tools.delegate_tool_registry → tui_gateway.server
```

So the copy boundary can be cut. There is no "we must copy 568k lines".

### 1.2 Why the core loop cannot be copied file by file

- `AIAgent` (`run_agent.py`, 1,592 lines) is assembled from 14 mixins. Its `__init__` takes about 70 parameters and forwards the real work to `agent/agent_init.py` (2,406 lines). `agent_init` is unreachable at module level and is imported lazily inside `__init__`, so copying `run_agent.py` yields a module that imports successfully and fails on instantiation.
- `agent/conversation_loop.py` (1,745 lines) is a coordinator. The actual logic lives in 15 `agent/turn_*.py` modules (5,444 lines).
- A function-granularity measurement over the 9 main files (a whole function counts as removable when its body mentions any feature outside this milestone) gives a 45% survival rate:

| File | Lines | Functions | Out of scope | After first pass |
|---|---:|---:|---:|---:|
| `run_agent.py` | 1,592 | 88 | 54 | 708 |
| `agent/conversation_loop.py` | 1,745 | 58 | 45 | 594 |
| `agent/tool_executor.py` | 1,849 | 80 | 51 | 550 |
| `agent/prompt_builder.py` | 1,767 | 65 | 17 | 1,438 |
| `tools/registry.py` | 1,007 | 70 | 21 | 558 |
| `model_tools.py` | 987 | 48 | 30 | 343 |
| `agent/system_prompt.py` | 817 | 41 | 26 | 305 |
| `agent/transports/chat_completions.py` | 672 | 32 | 23 | 178 |
| `tools/web_tools.py` | 549 | 24 | 12 | 279 |
| **Total** | **10,985** | | | **4,953 (45%)** |

Three further reductions follow the first pass: cascading deletion (the surviving code calls plenty of removed functions), flattening the mixins, and slimming the prompt text. The expected v0.1 result is **2,000–3,000 lines**.

---

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | The copy boundary is L0+L1; the L2 platform layer is not copied | See §3 |
| D2 | Copy everything into `vendor/` first, then trim and move out module by module | Mainline code stays runnable at every point; the "unrunnable" state exists only inside `vendor/` |
| D3 | `vendor/` is not committed | Committing it would add a 25,000-line diff. MIT attribution is satisfied by the per-file copyright headers plus [Porting from Hermes](../development/porting-from-hermes.md) in the development guidelines |
| D4 | Flat root layout, aligned with Hermes filenames and import roots | See §5 |
| D5 | No wheel is published; the project ships as a standalone product run from a checkout or Docker | A precondition for D4, and the project was never meant to be depended on as a library |
| D6 | v0.1 is split into 0.1.0 / 0.1.1 / 0.1.2; this branch delivers v0.1.0 only | See §8 |
| D7 | v0.1.0 exercises the tool path with a placeholder `get_time` tool; `web_search` waits for v0.1.2 | Keeps v0.1.0's tests free of any network dependency |

---

## 3. Copy boundary

| Layer | Contents | Lines | Treatment |
|---|---|---:|---|
| **L0** interface leaves | `agent/transports/base.py`, `agent/transports/types.py`, `agent/iteration_budget.py`, `agent/retry_utils.py`, `agent/web_search_provider.py`, `tools/interrupt.py` | 601 | **Copied verbatim**, MIT copyright headers kept |
| **L1** loop skeleton | `conversation_loop.py` + 15 `turn_*.py` + `tool_executor.py` + `agent_init.py` + `registry.py` + `chat_completions.py` + `model_tools.py` + 12 `AIAgent` mixins and others, 44 files in all | 24,754 | **Copied into vendor, then trimmed** — this is the part that has to be understood |
| **L2** platform layer | `agent_runtime_helpers.py` (3,509), `model_metadata.py` (2,551), `turn_recovery.py` (1,813), `error_classifier.py` (1,394), `redact.py` (1,335), `display.py` (1,118), `hermes_constants.py` (1,515), `hermes_logging.py` (764) and others | ~36,000 | **Not copied**; written from scratch as needed |

L0/L1/L2 is the **copy boundary** (what goes into vendor and what does not); it is a different axis from the milestone split in §8. All six L0 files are inside the boundary but land at different times: `transports/base.py`, `transports/types.py` and `iteration_budget.py` in v0.1.0; `retry_utils.py` and `tools/interrupt.py` in v0.1.1; `web_search_provider.py` in v0.1.2.

L2 is not the agent loop. It is model metadata tables, error classification, redaction, terminal rendering and global constants. For example, the OpenAI client is constructed in `agent_runtime_helpers.create_openai_client`; Mertina writes its own in about 30 lines.

The `turn_*` separation inside L1 is kept rather than merged into one large loop file, because v0.2–v0.5 add capabilities back (compression, memory injection, approval) and those need somewhere to go.

---

## 4. The vendor workflow

```
vendor/hermes/        <- L0+L1 copied verbatim; excluded from the build, from lint, and from git
agent/ tools/ ...     <- trimmed code moved out, runnable at all times
```

Move-out order; each step runs before the next begins:

1. L0 leaves (verbatim)
2. `agent/transports/`
3. `tools/registry.py` + `model_tools.py`
4. `agent/tool_executor.py` and tool dispatch
5. `agent/conversation_loop.py` + `agent/turn_*.py`
6. `run_agent.py` (mixins flattened)

`vendor/` is deleted entirely at the end.

`vendor/` is added to `.gitignore`. The upstream SHA, the naming rules and the deviation log live in [Porting from Hermes](../development/porting-from-hermes.md) in the development guidelines, which is a standing document.

The file-by-file progress checklist for this port is one-shot. It lives at `vendor/PORT_CHECKLIST.md` (also out of git) and is deleted with `vendor/`. Because the naming rules in §5.1 keep paths 1:1, no file-by-file mapping table is needed in the long run.

---

## 5. Layout alignment

### 5.1 Rules

Hermes uses a flat root layout: `agent/`, `tools/`, `gateway/`, `cron/` and `hermes_cli/` sit at the repository root, alongside root-level single-file modules (`run_agent.py`, `model_tools.py`, `utils.py`, `hermes_state*.py`). Its `setup.py` actively blocks wheel and sdist builds outside a Nix build, because runtime assets are resolved through the source-checkout layout. That is also why it can take generic top-level package names like `agent` and `tools`.

Mertina takes the same shape, under two rules:

1. **Paths and filenames are preserved**: `agent/turn_tool_round.py` maps to `agent/turn_tool_round.py`
2. **The `hermes_*` prefix is mechanically replaced with `mertina_*`**: `hermes_state.py` becomes `mertina_state.py`, `hermes_cli/` becomes `mertina_cli/`

This makes `diff hermes-agent/agent/turn_tool_round.py mertina-agent/agent/turn_tool_round.py` a pure semantic diff, with import statements identical on both sides.

The value of alignment is not evenly distributed; the breakdown is recorded here for future trade-offs:

| What is aligned | Value | Cost |
|---|---|---|
| Filenames / module names | High (a ported file diffs directly against its upstream twin) | Close to zero |
| Import roots (`agent.` rather than `mertina_agent.agent.`) | High (imports sit at the top of every file; misalignment makes every diff start with noise) | Giving up pip installation |
| Directory nesting depth | Close to zero (Hermes is already flat; there is no structure to inherit) | — |
| Accretion patterns | Negative (they are debt, not an asset) | — |

### 5.2 Accretion is not inherited

28 of the 45 root-level `.py` files in Hermes are `hermes_state_*.py`. Of the 238 `.py` files under `agent/`, only 10 subdirectories exist, while 31 files are `turn_*.py`, 8 are `auxiliary_*.py` and 7 are `context_*.py`. This is decomposition in place: a file grew too large and was split into siblings, and the shared prefix is the directory that was never created.

**Mertina does not replicate those split marks up front.** Copying `hermes_state.py` yields a single `mertina_state.py`. If it later needs splitting, it becomes a `state/` package, and the deviation is recorded in [Porting from Hermes](../development/porting-from-hermes.md) (for example `hermes_state_*.py (28) → state/`).

Files Mertina writes itself, with no upstream counterpart, are organised however suits them. No correspondence is invented.

### 5.3 Conflict with the README

Lines 76–89 of the README describe a `src/mertina_agent/` layout, which conflicts with D4. This branch updates that section.

---

## 6. Dependencies

Scanning the files inside the v0.1 copy boundary for third-party imports (including lazy ones):

| Package | Where | v0.1 |
|---|---|---|
| `openai` | Not inside the boundary (client construction lives in L2's `agent_runtime_helpers.py`) | **Needed in v0.1.0**; about 30 lines of our own construction code |
| `fire` | The CLI entry point in `run_agent.py` | Not needed; the standard library's `argparse` replaces it |
| `httpx` | `tools/web_tools.py` | Ships with the openai SDK; not listed separately |
| `ddgs` | `tools/web_tools.py` | v0.1.2 (keyless search provider) |
| `pyyaml` | `hermes_cli/config.py` | v0.1.2 (config file) |

Hermes lists 36 core dependencies (`rich`, `tenacity`, `pydantic`, `fastapi`, `Pillow`, `croniter`, `PyJWT` and others). **v0.1.0 needs none of them and depends only on `openai`.** Note that `tenacity` is not used on Hermes's retry path either — `retry_utils.py` is hand-written backoff.

Python version follows Hermes: `>=3.11`.

---

## 7. v0.1.0 design

### 7.1 Modules

| Path | Origin | Notes |
|---|---|---|
| `agent/transports/base.py` | L0 verbatim | The `ProviderTransport` ABC |
| `agent/transports/types.py` | L0 verbatim | `ToolCall` / `Usage` / `NormalizedResponse`, minus the codex/bedrock/anthropic `provider_data` accessors |
| `agent/transports/__init__.py` | L1 trimmed | Transport registry, registering `chat_completions` only |
| `agent/transports/chat_completions.py` | L1 trimmed | Keeps sanitize → build_kwargs → normalize_response; vendor-specific special cases removed |
| `agent/iteration_budget.py` | L0 verbatim | `normalize_budget_warning_ratio` dropped |
| `agent/conversation_loop.py` | L1 trimmed | The `run_conversation()` entry point and turn scheduling |
| `agent/turn_api_request.py` | L1 trimmed | Request assembly |
| `agent/turn_response_intake.py` | L1 trimmed | Response normalization |
| `agent/turn_tool_round.py` | L1 trimmed | One round of tool calls |
| `agent/turn_finalizer.py` | L1 trimmed | Turn wrap-up |
| `agent/tool_executor.py` | L1 trimmed | Parallel execution of independent tool calls; approval gate, middleware, checkpoints and heartbeats removed |
| `tools/registry.py` | L1 trimmed | Keeps the `ToolEntry` shape and `register()`; plugin scoping, the discovery cache and the `check_fn` cache removed |
| `tools/time_tools.py` | New | The `get_time` placeholder tool |
| `model_tools.py` | L1 trimmed | Tool definition collection and dispatch; toolset selection, hooks and bridges removed |
| `run_agent.py` | L1 trimmed | `AIAgent` with its 14 mixins flattened and `__init__` narrowed to this milestone's parameters |

### 7.2 Data flow

```
user_message
  -> assemble messages (system prompt: identity + tools + time)
  -> transport.build_kwargs()       -> OpenAI-compatible endpoint
  -> transport.normalize_response() -> NormalizedResponse
  -> finish_reason == "tool_calls"?
       yes -> look up in registry -> run independent calls in parallel -> append tool results -> back to the model call
       no  -> return the final text
  IterationBudget.consume() before each round; exhaustion ends the turn with a "budget exhausted" result
```

Two entry points, as the Roadmap specifies: `chat()` returns the final text, `run_conversation()` returns messages, metadata and usage.

### 7.3 Errors, interruption, budget

- **Retry**: 429 and 5xx go through `jittered_backoff`, honouring the `Retry-After` header, with a configurable retry ceiling. Full error classification and fallback are left to P1.
- **Interrupt**: a stop flag is checked before every model call. On stop, tool calls that were issued but never completed get an `"interrupted"` result, so the history stays valid for the model API.
- **Budget**: `IterationBudget.consume()` per round.

v0.1.0 implements the budget and the basic stop flag only; retry and mid-stream interruption arrive in v0.1.1.

### 7.4 Tests

All tests use a fake model client and make no network calls:

- A plain text answer (no tool calls)
- A single tool call followed by an answer
- Parallel tool calls (several independent tool calls in one round)
- The wrap-up when the iteration budget is exhausted
- A stop in the middle of a turn, after which the conversation can continue (valid history)

The `get_time` tool needs no network, so the tool path is covered end to end.

### 7.5 Acceptance

- A script runs the full loop against the fake client, including a tool-call round trip
- The five unit tests above pass
- `vendor/` has been removed from the working tree, and the upstream SHA in the development guidelines is up to date
- The README's layout section matches the actual layout

---

## 8. Milestone split

| | Contents | Result |
|---|---|---|
| **v0.1.0** (this branch) | L0 leaves + transports + registry + loop + `get_time` | The full loop runs against a fake client |
| v0.1.1 | Streaming + retry + interruption (`tools/interrupt.py`, `agent/prompt_builder.py`, `agent/system_prompt.py`) | The Roadmap's stop semantics are met |
| v0.1.2 | `web_search` + the `WebSearchProvider` interface + the config layer (`mertina_cli/config.py`) | The Roadmap's acceptance scenario runs against a real endpoint |

When v0.1.2 lands, all three of the Roadmap's v0.1 "Done when" items are satisfied.

---

## 9. Open questions

- **How deep the L2 replacements go**: error classification, redaction and logging get minimal implementations in v0.1.0. Whether to revisit Hermes's counterparts is a P1 question.
- **The risk of the `agent` / `tools` top-level names**: in a virtualenv they could in principle collide with a third-party package of the same name. Hermes has lived with this risk. If a collision ever happens, the fallback is to add the `src/mertina_agent/` prefix (D4's alternative), at the cost of a permanent difference from upstream on every import line.
- **No mechanism for tracking upstream**: the development guidelines record the upstream SHA, but nothing yet detects that an already-ported file changed upstream. A `scripts/diff-hermes.sh` is the candidate; it will be assessed after v0.1.2.
