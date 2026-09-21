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

### 2. Keep the copyright header

A file copied verbatim keeps its original copyright header, as the MIT License requires. A file
that was read and rewritten carries a note naming the upstream file it descends from.

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

With a Hermes checkout alongside this repository, a ported file diffs directly against its twin:

```bash
diff <(sed 's/^from \(agent\|tools\)\./from mertina.\1./' ../hermes-agent/agent/turn_tool_round.py) \
     mertina/agent/turn_tool_round.py
```

The `sed` filter rewrites upstream's import roots to ours, so the only differences left are
semantic ones.

Remember that our copy is deliberately smaller: features outside the current milestone were removed
on purpose, and a large diff is the expected result, not a problem to fix.
