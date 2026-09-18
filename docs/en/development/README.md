# Development Guidelines

**English** | [中文](../../zh/development/README.md)

This directory holds the development guidelines for Mertina Agent, a cloud-native version of Hermes Agent.
Everyone who contributes to the project, maintainers included, follows these guidelines.

## Documents

| Document | What it covers |
|---|---|
| [Contributing](contributing.md) | Where to start: setting up the environment and the end-to-end contribution workflow |
| [Governance](governance.md) | Roles, permissions, how decisions are made, how to become a collaborator |
| [Branching](branching.md) | Branch model, branch naming, the protection rules on `main` |
| [Commit Convention](commit-convention.md) | Conventional Commits format, types, examples |
| [Pull Requests](pull-requests.md) | How to open, update and merge a pull request |
| [Code Review](code-review.md) | What reviewers look for and how authors and reviewers work together |
| [Coding Style](coding-style.md) | Python style, typing, project layout, dependencies |
| [Testing](testing.md) | Test layout, what must be tested, how to run tests |
| [Versioning and Release](versioning-and-release.md) | SemVer, tags, changelog, release flow |
| [Issues and Labels](issues-and-labels.md) | How to report bugs, request features, and how issues are triaged |
| [Security](security.md) | Handling secrets, reporting vulnerabilities, security rules for a cloud service |

## The rules in brief

1. `main` is the only long-lived branch. It must always build and pass tests.
2. Nobody pushes to `main` directly, maintainers included. Every change goes through a pull request.
3. Name branches `<type>/<short-description>`, for example `feat/s3-session-store`.
4. PR titles follow [Conventional Commits](https://www.conventionalcommits.org/), for example `feat: add S3 session store`.
5. Each PR needs at least one approval from someone other than its author, and CI must pass.
6. PRs are merged with **squash merge** only. The branch is deleted automatically afterwards.
7. Versions follow [SemVer](https://semver.org/). Git tags (`vX.Y.Z`) are the single source of truth for versions.
8. Never commit secrets. Report vulnerabilities privately, not in public issues.
9. AI tools are welcome, but a human must understand and take responsibility for every contribution.
10. Everyone follows the [Code of Conduct](../../../CODE_OF_CONDUCT.md).

## Status

Some of the tooling these guidelines refer to (CI, pre-commit hooks, release automation, PR and issue templates) is still being set up.
Until it lands, apply the same rules by hand. When a guideline and the tooling disagree, open an issue so we can fix one of them.

## Changing these guidelines

The guidelines follow the same process as code: open a PR with the `docs:` type and update the English and Chinese versions in the same PR.
Changes that alter the process itself (for example the review or release rules) need approval from a maintainer.
