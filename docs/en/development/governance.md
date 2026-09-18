# Governance

**English** | [中文](../../zh/development/governance.md)

This document describes who can do what in the project and how decisions are made.
The model is intentionally lightweight and will evolve as the project grows.

## Roles

| Role | Who | Can |
|---|---|---|
| **Contributor** | Anyone | Open issues and PRs (from a fork), comment, review |
| **Collaborator** | People invited to the repository | Everything above, plus push branches to the main repository, approve PRs, merge approved PRs, triage issues |
| **Maintainer** | Repository owner (currently @boring-mars) | Everything above, plus change repository settings and branch protection, manage collaborators, cut releases, make final decisions |

The repository is currently owned by a personal account, so the owner is the only person with admin rights.
If the project moves to a GitHub organization, the maintainer role will be extended to more people.

## Rules that apply to everyone

Branch protection on `main` applies to every role, including the maintainer. Nobody can bypass it:

- No direct pushes, force pushes or deletions on `main`
- Every change goes through a PR
- Every PR needs at least one approval from someone other than its author
- Only squash merge is allowed

## How decisions are made

- **Everyday changes** (bug fixes, small features, docs): decided in the PR. One approval plus green CI is enough.
- **Significant changes** (new features, public API changes, new dependencies, architecture changes):
  open an issue or Discussion first. Give people at least a few days to respond before starting large work.
- **Design decisions worth recording** (for example the choice of storage backend or the deployment model):
  write an Architecture Decision Record (ADR) and submit it as a PR. ADRs live in `docs/adr/`,
  are numbered in order (`0001-use-postgres-for-sessions.md`), and describe the context, the options considered,
  the decision and its consequences. An accepted ADR is not edited later. A new ADR supersedes it instead.
- **Disagreements**: try to reach consensus in the discussion first. If that fails, the maintainer makes the final call
  and writes down the reasoning.

## Becoming a collaborator

There is no fixed quota. Contributors are usually invited after they have:

- Had several non-trivial PRs merged
- Reviewed other people's PRs constructively
- Shown that they understand and follow these guidelines

Any collaborator can suggest a candidate to the maintainer. Collaborators who have been inactive for a long time
(for example six months) may have write access removed. They are welcome back at any time.

## Relationship with upstream

Mertina Agent is derived from Hermes Agent. Decisions about whether and how to follow upstream changes
(for example syncing upstream releases or porting specific fixes) are significant changes and follow the process above.
Code taken from upstream keeps its original copyright and license notices.
