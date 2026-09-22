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

- `AIAgent` (`run_agent.py`, 1,609 lines) is assembled from 14 mixins. Its `__init__` takes about 70 parameters and forwards the real work to `agent/agent_init.py` (2,466 lines). `agent_init` is unreachable at module level and is imported lazily inside `__init__`, so copying `run_agent.py` yields a module that imports successfully and fails on instantiation.
- `agent/conversation_loop.py` (1,745 lines) is a coordinator. The actual logic lives in 15 `agent/turn_*.py` modules (5,444 lines).
- A function-granularity measurement over the 9 main files (a whole function counts as removable when its body mentions any feature outside this milestone) gives a 45% survival rate:

| File | Lines | Functions | Out of scope | After first pass |
|---|---:|---:|---:|---:|
| `run_agent.py` | 1,609 | 88 | 54 | 708 |
| `agent/conversation_loop.py` | 1,745 | 58 | 45 | 594 |
| `agent/tool_executor.py` | 1,856 | 80 | 51 | 550 |
| `agent/prompt_builder.py` | 1,767 | 65 | 17 | 1,438 |
| `tools/registry.py` | 1,012 | 70 | 21 | 558 |
| `model_tools.py` | 987 | 48 | 30 | 343 |
| `agent/system_prompt.py` | 817 | 41 | 26 | 305 |
| `agent/transports/chat_completions.py` | 692 | 32 | 23 | 178 |
| `tools/web_tools.py` | 569 | 24 | 12 | 279 |
| **Total** | **11,054** | | | **4,953 (45%)** |

Three further reductions follow the first pass: cascading deletion (the surviving code calls plenty of removed functions), flattening the mixins, and slimming the prompt text. The expected v0.1 result is **2,000–3,000 lines**.

---

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | The copy boundary is L0+L1; the L2 platform layer is not copied wholesale, and the functions we reach are ported into files of the same path | See §3 |
| D2 | Copy everything into `vendor/` first, then trim and move out module by module | Mainline code stays runnable at every point; the "unrunnable" state exists only inside `vendor/` |
| D3 | `vendor/` is not committed | Committing it would add a 25,000-line diff. MIT attribution is satisfied by the per-file copyright headers plus [Porting from Hermes](../development/porting-from-hermes.md) in the development guidelines |
| D4 | Flat layout (no `src/`) with exactly one top-level package, `mertina`, mirroring Hermes inside it | See §5 |
| D5 | No wheel is published; the project ships as a standalone product run from a checkout or Docker (`[tool.uv] package = false`) | It is an application, not a library; this is also why there is no `src/` layout |
| D6 | v0.1 is split into 0.1.0 / 0.1.1 / 0.1.2; this branch delivers v0.1.0 only | See §8 |
| D7 | v0.1.0 exercises the tool path with a placeholder `get_time` tool; `web_search` waits for v0.1.2 | Keeps v0.1.0's tests free of any network dependency |

---

## 3. Copy boundary

| Layer | Contents | Lines | Treatment |
|---|---|---:|---|
| **L0** interface leaves | `agent/transports/base.py`, `agent/transports/types.py`, `agent/iteration_budget.py`, `agent/retry_utils.py`, `agent/web_search_provider.py`, `tools/interrupt.py` | 601 | **Copied verbatim in substance** (defined in rule 2 of [Porting from Hermes](../development/porting-from-hermes.md)), with a provenance header; out-of-scope features are cut in a separate commit right after, and §7.1 says what each file loses |
| **L1** loop skeleton | `conversation_loop.py` + 15 `turn_*.py` + `tool_executor.py` + `agent_init.py` + `registry.py` + `chat_completions.py` + `model_tools.py` + 14 `AIAgent` mixins + `hermes_cli/config.py` + the ddgs provider under `plugins/web/` and others, 46 files in all | 28,234 | **Copied into vendor, then trimmed** — this is the part that has to be understood |
| **L2** platform layer | `agent_runtime_helpers.py` (3,509), `model_metadata.py` (2,551), `turn_recovery.py` (1,813), `error_classifier.py` (1,394), `redact.py` (1,335), `display.py` (1,118), `hermes_constants.py` (1,515), `hermes_logging.py` (764) and others | ~36,000 | **Not copied wholesale**; when the loop reaches a function, that function alone is copied verbatim into a file of the same path (rule 5 of [Porting from Hermes](../development/porting-from-hermes.md)) |

L0/L1/L2 is the **copy boundary** (what goes into vendor and what does not); it is a different axis from the milestone split in §8. All six L0 files are inside the boundary but land at different times: `transports/base.py`, `transports/types.py` and `iteration_budget.py` in v0.1.0; `retry_utils.py` and `tools/interrupt.py` in v0.1.1; `web_search_provider.py` in v0.1.2.

L2 is not the agent loop. It is model metadata tables, error classification, redaction, terminal rendering and global constants. For example, the OpenAI client is constructed in `agent_runtime_helpers.create_openai_client`. v0.1.0 ports that one function, and the lazy `process_bootstrap.OpenAI` proxy it uses, into files of the same path, and leaves the thousands of other lines in those files alone.

The `turn_*` separation inside L1 is kept rather than merged into one large loop file, because v0.2–v0.5 add capabilities back (compression, memory injection, approval) and those need somewhere to go.

---

## 4. The vendor workflow

```
vendor/hermes/        <- L0+L1 copied verbatim; excluded from the build, from lint, and from git
mertina/              <- trimmed code moved out, runnable at all times (agent/, tools/ live inside)
```

Move-out order; each step runs before the next begins:

1. L0 leaves (verbatim in substance, with the cuts in a commit of their own)
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

**Mertina does not copy that last part.** After surveying comparable projects (below), the repository
root holds exactly one import package:

```
mertina-agent/            repository and distribution name
├── mertina/              the one top-level import package
│   ├── agent/            <-> hermes agent/
│   ├── tools/            <-> hermes tools/
│   ├── run_agent.py      <-> hermes run_agent.py
│   └── model_tools.py    <-> hermes model_tools.py
├── tests/unit/           mirrors mertina/
└── pyproject.toml
```

Three mapping rules:

1. **Prefix the upstream path with `mertina/`**: `agent/turn_tool_round.py` becomes `mertina/agent/turn_tool_round.py`
2. **Filenames are unchanged**
3. **The `hermes_` prefix is dropped** (not translated to `mertina_`; the package name already supplies the namespace): `hermes_state.py` becomes `mertina/state.py`, `hermes_cli/` becomes `mertina/cli/`

A `sed` filter flattens the difference in import roots when comparing, leaving only semantic differences:

```bash
diff <(sed 's/^from \(agent\|tools\)\./from mertina.\1./' ../hermes-agent/agent/turn_tool_round.py) \
     mertina/agent/turn_tool_round.py
```

#### Why Hermes's top-level layout is not copied

"Flat versus src" and "one named top-level package versus several generic ones" are independent
questions. What makes Hermes unusual is the second.

Comparable projects, all of them flat with a single named top-level package:

| Project | Shape | Top-level import package |
|---|---|---|
| Home Assistant (90.8k★) | A long-running service, the closest match to Mertina | `homeassistant/` |
| Aider (49.1k★) | An AI agent CLI application | `aider/` (distributed as `aider-chat`) |
| SWE-agent (20.4k★) | An AI agent | `sweagent/` |
| Django | A framework | `django/` |
| Flask / black / pip | Libraries | `src/<name>/` |

SWE-agent also has a root-level `tools/`, but it is a directory of tool assets, not an import
package — the same shape as the flat-layout example in PyPA's
[src layout vs flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/),
where the root holds one named import package and `tools/` holds scripts.

Meanwhile `agent` and `tools` are both **real packages on PyPI** (`agent 0.1.3`, `tools 1.0.35`),
each providing a top-level module of that name. Taking those names is a concrete collision risk,
not a theoretical one.

Why no `src/`: the benefits of a src layout (preventing accidental imports of the in-development
copy, forcing the installed copy to be used) exist for libraries that get distributed. Under D5 we
publish nothing and set `[tool.uv] package = false`, so there is no install step, and a src layout
would instead make the code unimportable without a path hack. The three application projects above
have no `src/` either.

Why `mertina` rather than `mertina_agent`: an import package shorter than its distribution name is
the norm (`aider-chat` → `aider`, `scikit-learn` → `sklearn`, `Pillow` → `PIL`). More importantly,
mirroring Hermes means the first level inside is `agent/`, so `mertina_agent.agent.conversation_loop`
stutters — and the package will later hold the gateway, state and cron as well.

The value of alignment is not evenly distributed; the breakdown is recorded here for future trade-offs:

| What is aligned | Value | Cost |
|---|---|---|
| Filenames / module names | High (a ported file diffs directly against its upstream twin) | Close to zero |
| Import roots | Moderate (one line of `sed` flattens it; not a structural cost) | Taking generic top-level names for it means collision risk and no packaging |
| Directory nesting depth | Close to zero (Hermes is already flat; there is no structure to inherit) | — |
| Accretion patterns | Negative (they are debt, not an asset) | — |

### 5.2 Accretion is not inherited

28 of the 45 root-level `.py` files in Hermes are `hermes_state_*.py`. Of the 238 `.py` files under `agent/`, only 10 subdirectories exist, while 31 files are `turn_*.py`, 8 are `auxiliary_*.py` and 7 are `context_*.py`. This is decomposition in place: a file grew too large and was split into siblings, and the shared prefix is the directory that was never created.

**Mertina does not replicate those split marks up front.** Copying `hermes_state.py` yields a single `mertina/state.py`. If it later needs splitting, it becomes a `mertina/state/` package, and the deviation is recorded in [Porting from Hermes](../development/porting-from-hermes.md) (for example `hermes_state_*.py (28) → state/`).

Files Mertina writes itself, with no upstream counterpart, are organised however suits them. No correspondence is invented.

### 5.3 Existing documents that need updating

These were written before this decision and assume a `src/mertina_agent/` layout. This branch corrects them:

| Document | What it said |
|---|---|
| `README.md` | The `src/mertina_agent/` tree under "Planned repository layout" |
| `coding-style.md` | The project layout tree, `mypy src`, "uses the src layout", the package name `mertina_agent`, `uv build` |
| `testing.md` | `--cov=mertina_agent`, and `tests/unit/` mirroring `src/mertina_agent/` |
| `versioning-and-release.md` | `mertina_agent` in the definition of the public interface |

---

## 6. Dependencies

Scanning the files inside the v0.1 copy boundary for third-party imports (including lazy ones):

| Package | Where | v0.1 |
|---|---|---|
| `openai` | Client construction lives in L2's `agent_runtime_helpers.create_openai_client`, ported partially | **Needed in v0.1.0** |
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
| `mertina/agent/transports/base.py` | L0 verbatim in substance | The `ProviderTransport` ABC, nothing cut |
| `mertina/agent/transports/types.py` | L0 verbatim in substance, then cut | `ToolCall` / `Usage` / `NormalizedResponse`, minus the Codex, Bedrock and Anthropic `provider_data` accessors (`call_id`, `response_item_id`, `anthropic_content_blocks`, `bedrock_content_blocks`, `codex_reasoning_items`, `codex_message_items`); `extra_content` (Gemini's `thought_signature`), `reasoning_content` and `reasoning_details` stay, since they also appear on OpenAI-compatible Chat Completions |
| `mertina/agent/transports/__init__.py` | L1 trimmed | Transport registry, registering `chat_completions` only |
| `mertina/agent/transports/chat_completions.py` | L1 trimmed | Keeps sanitize → build_kwargs → normalize_response; vendor-specific special cases removed |
| `mertina/agent/client_lifecycle.py` | L1 trimmed | Locking, the closed check, closing, and recreating the shared client once closed; construction is forwarded through `_forward` to `agent_runtime_helpers.create_openai_client`, as upstream does |
| `mertina/agent/agent_runtime_helpers.py` | L2, partial | Only `_ra()` and `create_openai_client`: copy the kwargs, `max_retries=0`, construct through the lazy proxy |
| `mertina/agent/process_bootstrap.py` | L2, partial | Only the lazy `OpenAI` proxy |
| `mertina/agent/lazy_forward.py` | Outside the boundary, whole | The `_forward` forwarder that lets a mixin delegate a method to a module-level function |
| `mertina/agent/__init__.py`, `mertina/agent/jiter_preload.py` | Outside the boundary, whole | Preloads the OpenAI SDK's native JSON parser at package import (on some Windows installs a first import in the streaming thread fails) |
| `mertina/tools/__init__.py` | Outside the boundary, verbatim then cut | Only the docstring saying that importing the tools package must have no side effects |
| `mertina/agent/iteration_budget.py` | L0 verbatim in substance, then cut | `normalize_budget_warning_ratio` dropped |
| `mertina/agent/conversation_loop.py` | L1 trimmed | The `run_conversation()` entry point and turn scheduling |
| `mertina/agent/turn_api_request.py` | L1 trimmed | Request assembly |
| `mertina/agent/turn_response_intake.py` | L1 trimmed | Response normalization |
| `mertina/agent/turn_tool_round.py` | L1 trimmed | One round of tool calls |
| `mertina/agent/turn_finalizer.py` | L1 trimmed | Turn wrap-up |
| `mertina/agent/turn_context.py` | L1 trimmed | Per-turn setup: append the user message, reset the iteration budget, build the system prompt; build the wire copy of the messages |
| `mertina/agent/turn_iteration_prep.py` | L1 trimmed | Interrupt check and budget consumption at the start of each iteration |
| `mertina/agent/turn_request_assembly.py` | L1 trimmed | Build `api_messages` |
| `mertina/agent/turn_api_call.py` | L1 trimmed | Issue the model request |
| `mertina/agent/turn_response_check.py` | L1 trimmed | Record latency, add up usage |
| `mertina/agent/turn_final_response.py` | L1 trimmed | Take the final answer when there are no tool calls |
| `mertina/agent/turn_loop_errors.py` | L1 trimmed | On a response-processing error, fill in tool results and end the turn |
| `mertina/agent/tool_executor.py` | L1 trimmed | Parallel execution of independent tool calls; approval gate, middleware, checkpoints and heartbeats removed |
| `mertina/tools/registry.py` | L1 trimmed | Keeps the `ToolEntry` shape and `register()`; plugin scoping, the discovery cache and the `check_fn` cache removed |
| `mertina/tools/time_tools.py` | New | The `get_time` placeholder tool |
| `mertina/model_tools.py` | L1 trimmed | Tool definition collection and dispatch; toolset selection, hooks and bridges removed |
| `mertina/run_agent.py` | L1 trimmed | `AIAgent` with its 14 mixins flattened and `__init__` narrowed to this milestone's parameters; until step 7, only the module logger that `_ra()` uses |

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
| v0.1.2 | `web_search` + the `WebSearchProvider` interface + the config layer (`mertina/cli/config.py`) | The Roadmap's acceptance scenario runs against a real endpoint |

When v0.1.2 lands, all three of the Roadmap's v0.1 "Done when" items are satisfied.

---

## 9. Open questions

- **How deep the L2 replacements go**: error classification, redaction and logging get minimal implementations in v0.1.0. Whether to revisit Hermes's counterparts is a P1 question.
- **Whether `mertina/` needs more internal structure**: v0.1 has only the `agent/` and `tools/` subpackages, which is small. Revisit once the gateway, state and cron arrive.
- **No mechanism for tracking upstream**: `scripts/port_check.py` keeps ported files matching the upstream SHA in their headers, but noticing that upstream changed an already-ported file in a newer commit is still manual. A mode of port_check that compares against a newer SHA is the candidate; it will be assessed after v0.1.2.
