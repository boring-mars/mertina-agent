# Contributing Guide

**English** | [中文](../../zh/development/contributing.md)

Thank you for your interest in Mertina Agent. This guide walks you from zero to a merged pull request.

## Ways to contribute

- Report bugs and suggest features through [issues](issues-and-labels.md)
- Improve documentation, including these guidelines
- Fix bugs or implement features. Issues labeled `good first issue` are a good place to start
- Review other people's pull requests. Comments from non-maintainers are welcome too

## Before you start

- **Search first.** Check existing issues and pull requests so work is not duplicated.
- **Discuss large changes first.** For a new feature, an architectural change or anything over a few hundred lines,
  open an issue (or a Discussion) and agree on the approach before you write code.
  Feature PRs without prior agreement may be closed, however well written.
- **Pick work that is ready.** Issues labeled `accepted`, `help wanted` or `good first issue` can be picked up
  without asking first. Avoid issues labeled `needs-discussion` until the approach is agreed.
- **Claim the issue.** Comment on the issue to say you are working on it, so others don't pick it up too.
  If you stop, say so, so it can be reassigned.
- Small fixes (typos, obvious bugs, small docs changes) can go straight to a PR.

## AI-assisted contributions

AI coding tools are welcome. This project is itself an AI agent. What matters is that a human understands
and stands behind every contribution:

- **You are responsible for what you submit.** Review, understand and test AI-generated code before you open a PR.
  You must be able to explain every change and answer reviewers' questions yourself.
- **Disclose significant AI assistance** in the PR description: which tool you used and for what
  (for example "tests drafted with an AI assistant, reviewed and edited by me").
  Autocomplete-level help does not need disclosure.
- **Write issues, PR descriptions and replies in your own words.** Don't paste raw AI output as a description or
  as an answer to a reviewer.
- **No unattended agents.** PRs, issues and comments created by autonomous agents without a human actively
  involved will be closed.
- AI-generated code is subject to the same licensing rules as any other code (see [Licensing](#licensing-and-attribution)).

## Development environment

### Prerequisites

- [Git](https://git-scm.com/)
- [uv](https://docs.astral.sh/uv/) — manages the Python version, the virtual environment and dependencies.
  You don't need to install Python separately; uv installs the version the project requires.

### Getting the code

Outside contributors work from a fork:

```bash
# 1. Fork boring-mars/mertina-agent on GitHub, then clone your fork
git clone https://github.com/<your-username>/mertina-agent.git
cd mertina-agent

# 2. Add the main repository as "upstream"
git remote add upstream https://github.com/boring-mars/mertina-agent.git
```

Collaborators with write access can clone the main repository directly and push branches to it.

### Installing and checking

```bash
uv sync                  # create .venv and install all dependencies from uv.lock
uv run pre-commit install  # install git hooks (lint and format before each commit)
uv run pytest            # run the tests
uv run ruff check .      # lint
uv run ruff format .     # format
uv run mypy src          # type check
```

If `uv sync` fails, or the tests fail on a fresh `main`, open an issue. That is a bug in the project, not in your setup.

## Workflow

```
main ──●──────────────●──────→
        \            ↗
         feat/xxx ──●──●        squash merge, then the branch is deleted
```

1. **Update `main`.**
   ```bash
   git switch main
   git pull upstream main     # collaborators: git pull origin main
   ```
2. **Create a branch.** See [Branching](branching.md) for naming.
   ```bash
   git switch -c feat/short-description
   ```
3. **Make your change.** Keep it focused on one thing. Add or update tests and docs.
4. **Commit.** Commit messages inside the branch are free-form, because the PR is squashed.
   Clear messages still help reviewers. See the [Commit Convention](commit-convention.md).
5. **Keep your branch current.** If `main` has moved on, rebase:
   ```bash
   git fetch upstream
   git rebase upstream/main
   git push --force-with-lease -u origin feat/short-description
   ```
   Force-pushing is fine on your own feature branch. It is never allowed on `main`.
6. **Push and open a PR.**
   ```bash
   git push -u origin feat/short-description
   ```
   Fill in the PR template. See [Pull Requests](pull-requests.md).
7. **Respond to review.** Push new commits to the same branch. The PR updates automatically.
8. **Merge.** Once approved and CI is green, a maintainer (or you, if you have write access) squash-merges the PR.
9. **Clean up locally.**
   ```bash
   git switch main
   git pull --prune
   git branch -d feat/short-description
   ```

## Licensing and attribution

- The project is released under the [MIT License](../../../LICENSE).
  By submitting a contribution you agree that it is licensed under the same terms.
- Only submit code you wrote yourself or that you have the right to submit under a compatible license.
- Mertina Agent is derived from Hermes Agent. When you copy or adapt code from Hermes Agent or any other project,
  keep the original copyright and license notices, and say where the code came from in the PR description.
- Do not add dependencies whose licenses are incompatible with MIT (for example GPL or AGPL) without discussing it first.

## Code of conduct

Everyone taking part in the project is expected to follow the [Code of Conduct](../../../CODE_OF_CONDUCT.md).
In short: be respectful and constructive, critique code rather than people, and remember that many contributors
are volunteers working in their spare time and in a second language.

## Getting help

- For questions about the code or the process, open an issue or a Discussion.
- If your PR has no response after a week, feel free to ping the maintainers in the PR.
  Reviews are done by volunteers, so please be patient.
