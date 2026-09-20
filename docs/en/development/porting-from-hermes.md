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
| Ported up to | `ec015c906c8758a17e1535810a45500dabd0e038` (2026-09-19) |

"Ported up to" is the upstream commit our current code was read from. Update it whenever a port
brings code across from a newer commit.

We do not track upstream continuously. Code is copied deliberately, at a known commit, and then
maintained as ours.

## Rules

### 1. Paths and filenames mirror upstream

A file ported from Hermes keeps its path and name: `agent/turn_tool_round.py` stays
`agent/turn_tool_round.py`. The `hermes_*` prefix is replaced with `mertina_*`, so
`hermes_state.py` becomes `mertina_state.py` and `hermes_cli/` becomes `mertina_cli/`.

Because of this, no file-by-file mapping table is needed: a ported file's origin is its own path.
`diff` against the matching upstream file is a pure semantic diff, with import statements identical
on both sides.

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

We do not replicate those split marks. Porting `hermes_state.py` yields one `mertina_state.py`.
If it later needs splitting, it becomes a `state/` package, and the deviation is recorded below.

## Deviations

Places where Mertina's structure departs from upstream, so the departure is not mistaken for drift.

| Upstream | Mertina | Why |
|---|---|---|
| (none yet) | | |

## Comparing against upstream

With a Hermes checkout alongside this repository, a ported file diffs directly against its twin:

```bash
diff ../hermes-agent/agent/turn_tool_round.py agent/turn_tool_round.py
```

Remember that our copy is deliberately smaller: features outside the current milestone were removed
on purpose, and a large diff is the expected result, not a problem to fix.
