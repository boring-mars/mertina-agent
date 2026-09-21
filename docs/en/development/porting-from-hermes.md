# Porting from Hermes

**English** | [中文](../../zh/development/porting-from-hermes.md)

Mertina Agent is a slimmed-down rebuild of [Hermes Agent](https://github.com/NousResearch/hermes-agent)
by Nous Research, released under the MIT License. This document is the standing record of what we
take from upstream and the rules for taking it. For the reasoning behind the rules, see
[the v0.1 agent core port design](../design/2026-09-20-agent-core-loop.md).

## Upstream

| | |
|---|---|
| Repository | https://github.com/NousResearch/hermes-agent |
| License | MIT |
| Ported up to | `fbc4ea8b96c784c9bb28dd27541c14514bba302d` (2026-09-20) |

"Ported up to" is the upstream commit our current code was read from. Update it whenever a port
brings code across from a newer commit.

We do not track upstream continuously. Code is copied deliberately, at a known commit, and then
maintained as ours.

## Rules

### 1. Paths and filenames mirror upstream, under the `mertina` package

Everything importable lives under the single top-level package `mertina`, so a ported file's path
is its upstream path with `mertina/` in front:

| Hermes | Mertina |
|---|---|
| `agent/turn_tool_round.py` | `mertina/agent/turn_tool_round.py` |
| `agent/transports/base.py` | `mertina/agent/transports/base.py` |
| `tools/registry.py` | `mertina/tools/registry.py` |
| `run_agent.py` | `mertina/run_agent.py` |
| `hermes_state.py` | `mertina/state.py` |
| `hermes_cli/config.py` | `mertina/cli/config.py` |

The `hermes_` prefix is dropped rather than translated: the package name already supplies the
namespace, so `mertina/mertina_state.py` would stutter.

Because the mapping is mechanical, no file-by-file table is needed — a ported file's origin is its
own path.

**Package `__init__.py` files come along.** Porting a file also ports the upstream `__init__.py` of every package directory above it. No package directory under `mertina/` may have a hand-written placeholder init when upstream has one; `scripts/port_check.py` fails on it. Only `mertina/__init__.py` itself has no upstream counterpart.

Files we write ourselves, with no upstream counterpart, are organised however suits them.
No correspondence is invented.

### 2. Copy verbatim in substance, and say where it came from

"Copied verbatim" means **verbatim in substance**: behavior and structure are unchanged, and the form
follows this repository's standards. Only four kinds of change are allowed:

1. Rewriting import roots by rule 1: `from agent.` → `from mertina.agent.`, `hermes_cli` → `mertina.cli`, `import hermes_bootstrap` → `from mertina import bootstrap`. **Module paths inside strings count too**, such as `_forward("agent.agent_runtime_helpers", ...)`, `importlib.import_module(f"agent.transports.{name}")` and `logging.getLogger("run_agent")`
2. The mechanical rewrites of `ruff check --fix` and `ruff format` (`Dict` → `dict`, re-wrapping, ...)
3. Completing type hints so mypy strict passes (such as `**kwargs` → `**kwargs: Any`), and wrapping
   over-long docstrings and comments without changing their wording
4. A `# noqa` or `# type: ignore` that keeps upstream's code as written, with the reason on the same line (such as keeping upstream's parameter name `id`, or two functions of different signatures bound to one name)

Cutting features that are out of scope is not part of the copy. It goes in a separate commit right
after it, so that commit's diff shows only what was removed.

A cut commit mostly deletes: whole lines, whole blocks, or the part of a line that belongs to an out-of-scope feature (an inline cut), with the rest left as it was. A few rewrites for the sake of a smaller file are allowed, such as reading a value directly instead of through a removed helper, but **each rewrite has a row in the deviations table below**. Before committing, run `uv run python scripts/port_check.py --cut-from <port commit>`. It sorts what the cut did not keep whole into `prose` (comments and docstrings), `inline` (inline cuts) and `rewrite`. The first two need no record; apart from changes 3 and 4, every `rewrite` must be in the deviations table.

Docstrings and comments are prose: no import rewriting, and after a cut they may be rewritten to match the code, which is not a departure. When code is deleted, a comment that only describes that code goes with it.

Every ported file opens with two lines naming its source:

```python
# Ported from hermes-agent agent/transports/base.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
```

A file that was read and rewritten rather than copied says `Derived from` instead of `Ported from`.

Upstream's `.py` files carry no copyright header of their own; the notice lives only in upstream's
root `LICENSE`. This repository's `LICENSE` lists both Nous Research's copyright line and ours, which
satisfies MIT's requirement that copies include the copyright and permission notice.

### 3. Keep abstractions, not implementations

Where Hermes has an interface with many implementations (model transports, search providers), port
the interface and implement the one we need.

### 4. Do not inherit accretion

Hermes has 28 `hermes_state_*.py` files at its repository root and 31 `agent/turn_*.py` files in one
flat directory. These are the result of splitting a file in place; the shared prefix is a directory
that was never created.

We do not replicate those split marks. Porting `hermes_state.py` yields one `mertina/state.py`.
If it later needs splitting, it becomes a `state/` package, and the deviation is recorded below.

### 5. Files outside the boundary (L2): port only what is reached

The L2 platform layer is not copied wholesale. When the loop really calls an L2 function, that function, with the helpers it calls in the same file, is copied verbatim into **upstream's path**, not into the caller's file and not rewritten. A later port of another function from the same file then only adds to it. Such files:

- carry a third header line, `# Partial: only the parts ported so far. Upstream order is kept.`
- keep the ported parts in upstream order
- keep only the imports the ported parts use; `ruff --fix` drops the rest
- are copied verbatim first and cut in a separate commit, like any other port

For example, `agent/agent_runtime_helpers.py` holds only `_ra()` and `create_openai_client`, `agent/process_bootstrap.py` only the lazy OpenAI proxy, and `run_agent.py`, until step 7, only its module logger.

## Deviations

Every rewrite in a cut commit (a line `port_check --cut-from` reports as `rewrite`) is recorded here so it is not mistaken for drift. Plain deletions, inline cuts, rewritten comments and docstrings, and rule 2's changes 1 to 4 are not recorded here.

| Upstream | Mertina | Why |
|---|---|---|
| `chat_completions._apply_max_tokens`: tries `ephemeral_max_output_tokens`, then `max_tokens` | `max_tokens` only: `candidate = params.get("max_tokens")` | `ephemeral_max_output_tokens` budgets upstream's internal tasks (titles, recovery), which v0.1 does not have |
| `chat_completions.convert_messages`: `reasoning_details` is replayed only to OpenRouter and Nous routes and to profiles that declare a native type | Always stripped on the wire: `strip_reasoning_details = True`; responses still parse it and history keeps it | **Behavior change.** Replaying per route is a provider special case, and keeping it means porting `utils.base_url_host_matches`. The cost is that OpenRouter's reasoning does not carry across turns |
| `chat_completions.build_kwargs`: the result goes through `_finish_kwargs`, which adds `prompt_cache_key` | `return api_kwargs` directly | Prompt-cache routing needs the Codex transport |
| `chat_completions.normalize_response`: `finish_reason` is folded by `normalize_finish_reason` (integer and upper-case values) | `_fr = ...` kept as is; the next line becomes `finish_reason = _fr or "stop"` | **Behavior change.** The folding targets Poolside and some Gemini gateways; keeping it means porting `message_sanitization` |
| `chat_completions.validate_response`: ends with `return not is_router_timeout_shim(response)` | `return True` | **Behavior change:** a router's fake success (HTTP 200 carrying a timeout message) is no longer recognized. Keeping it keeps four more definitions |
| `registry.ToolRegistry.register`: `target = self._slot(scope, create=True)` picks the table by profile scope | `target = self._tools` | Plugin and profile scopes are outside v0.1, so every tool registers in the global table |
| `model_tools._compute_tool_definitions`: `tools_to_include = _select_tool_names(enabled_toolsets, disabled_toolsets, quiet_mode)` selects by toolset | `tools_to_include = set(registry.get_all_tool_names())` | **Behavior change:** toolsets are no longer enabled or disabled; every registered tool goes to the model. Toolset selection needs the static tables in `toolsets.py`, and v0.1 has one tool, `get_time` |
| `tool_executor._run_agent_tool_execution_middleware`: `state.result, _relay_args = relay_tools.execute(function_name, function_args, _hermes_pipeline, ...)` dispatches through Relay and the tool middleware | `state.result = _authorized_dispatch(function_args)` | Relay and the tool middleware layer are outside v0.1; the call is dispatched once directly |
| `tool_executor.execute_tool_calls_sequential`: splits by terminal approval and calls `_execute_tool_calls_sequential` per run with `SimpleNamespace(tool_calls=calls)` | Passes `assistant_message` in one call | Terminal approval batches are outside v0.1 |

## Comparing against upstream

Before each port commit, with a Hermes checkout beside this repository, run:

```bash
uv run python scripts/port_check.py
```

It reads each upstream file at the SHA its ported file's header records (`git show <sha>:<path>`, so the checkout's branch does not matter), applies changes 1 and 2 of rule 2, and compares line by line:

- It lists every line that is not in upstream. Cuts leave none, so each one should trace back to the provenance header, changes 3 and 4, or a row of the deviations table
- It fails, with a non-zero exit status, when kept definitions are out of upstream order, when an import root was not rewritten (module paths inside strings included), or when a package `__init__.py` was not ported

With `--cut-from <port commit>` it also lists what the cut did not keep whole, sorted into `prose`, `inline` and `rewrite` (see rule 2).

Remember that our copy is deliberately smaller: features outside the current milestone were removed
on purpose, and a large diff is the expected result, not a problem to fix.
