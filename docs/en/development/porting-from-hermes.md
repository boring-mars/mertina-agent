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

Files we write ourselves, with no upstream counterpart, are organised however suits them.
No correspondence is invented.

### 2. Copy verbatim in substance, and say where it came from

"Copied verbatim" means **verbatim in substance**: behavior and structure are unchanged, and the form
follows this repository's standards. Only four kinds of change are allowed:

1. Rewriting the import root: `from agent.` → `from mertina.agent.`
2. The mechanical rewrites of `ruff check --fix` and `ruff format` (`Dict` → `dict`, re-wrapping, ...)
3. Completing type hints so mypy strict passes (such as `**kwargs` → `**kwargs: Any`), and wrapping
   over-long docstrings and comments without changing their wording
4. A `# noqa` that keeps an upstream signature, with the reason on the same line

Cutting features that are out of scope is not part of the copy. It goes in a separate commit right
after it, so that commit's diff shows only what was removed.

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

## Deviations

Places where Mertina's structure departs from upstream, so the departure is not mistaken for drift.

| Upstream | Mertina | Why |
|---|---|---|
| (none yet) | | |

## Comparing against upstream

With a Hermes checkout alongside this repository, a ported file diffs against its twin. Put the
upstream file through changes 1 and 2 of rule 2 first, and those two kinds of difference disappear:

```bash
f=agent/turn_tool_round.py
tmp=$(mktemp --suffix=.py)
sed 's/^from \(agent\|tools\)\./from mertina.\1./' ../hermes-agent/$f > "$tmp"
uv run ruff check --config pyproject.toml --fix-only --quiet "$tmp"
uv run ruff format --config pyproject.toml --quiet "$tmp"
diff "$tmp" mertina/$f
```

What remains is the provenance header, changes 3 and 4, and the features that were cut.

Remember that our copy is deliberately smaller: features outside the current milestone were removed
on purpose, and a large diff is the expected result, not a problem to fix.
