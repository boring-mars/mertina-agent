# Branching

**English** | [中文](../../zh/development/branching.md)

The project uses [GitHub Flow](https://docs.github.com/en/get-started/using-github/github-flow): one long-lived branch
plus short-lived feature branches.

## Branch model

```
main ──●────────●────────●──────→
        \      ↗ \      ↗
         feat/a    fix/b          squash merge via PR, then the branch is deleted
```

- **`main`** is the only long-lived branch. It must always build, pass tests, and be releasable.
- **Everything else is short-lived.** Branch off `main`, merge back with a PR, and the branch is deleted automatically.
  Aim to merge within days, not weeks. Long-running branches drift from `main` and become painful to merge.
- There are **no** `develop`, `staging` or `hotfix` branches. Fixes follow the same flow as any other change.

## Branch naming

Format: `<type>/<short-description>`

- `type` is the same as the [commit type](commit-convention.md#types)
- `short-description` is lowercase English words joined by hyphens (kebab-case). Keep it short but meaningful
- Optionally prefix the description with an issue number: `fix/42-session-timeout`
- No Chinese characters, spaces, uppercase letters or personal names

| Prefix | Use for | Example |
|---|---|---|
| `feat/` | New features | `feat/s3-session-store` |
| `fix/` | Bug fixes | `fix/42-session-timeout` |
| `docs/` | Documentation only | `docs/development-guidelines` |
| `refactor/` | Code changes that neither fix a bug nor add a feature | `refactor/split-agent-runner` |
| `perf/` | Performance improvements | `perf/cache-tool-schemas` |
| `test/` | Adding or fixing tests | `test/api-integration` |
| `build/` | Build system, packaging, dependencies | `build/switch-to-uv-build` |
| `ci/` | CI configuration | `ci/add-python-matrix` |
| `chore/` | Other maintenance | `chore/project-scaffold` |

Branches created by bots (Dependabot, Renovate, release-please) use their own naming and are exempt.

## Where branches live

- **Outside contributors** push branches to their own fork and open PRs from there.
- **Collaborators** may push branches directly to the main repository. This makes it easy for others to check out
  the branch and help. Only push branches you are actively working on.

## Protection rules on `main`

These are enforced by GitHub and apply to everyone, including administrators:

- A pull request is required. Direct pushes are rejected
- At least 1 approval is required, and approvals are dismissed when new commits are pushed
- All review conversations must be resolved
- Required CI status checks must pass (once CI is set up)
- Linear history is required: merge commits are not allowed
- Force pushes and deletion are not allowed

Repository settings: only **squash merge** is enabled, and **head branches are deleted automatically** after merge.

## Keeping a branch up to date

Prefer rebasing onto `main` over merging `main` into your branch:

```bash
git fetch origin            # or: git fetch upstream
git rebase origin/main
git push --force-with-lease
```

Use `--force-with-lease` rather than `--force`, so you don't overwrite commits someone else pushed to your branch.
If others are also committing to the same branch, agree on it with them before rebasing.

## Maintenance branches (future)

Right now releases are marked with tags only. No release branches exist.

If the project ever needs to patch an older major version while `main` moves on (for example ship `1.4.1`
while `main` is working on `2.0`), a maintenance branch is created from the release tag:

```bash
git switch -c release/1.x v1.4.0
```

Rules for maintenance branches:

- Named `release/<major>.x`
- Protected like `main`
- Receive bug fixes and security fixes only, never new features
- Fixes land on `main` first and are then cherry-picked (`git cherry-pick -x <sha>`) into the maintenance branch
- Patch releases are tagged from the maintenance branch
- The branch is archived (locked) when that version reaches end of life
