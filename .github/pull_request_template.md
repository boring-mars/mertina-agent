<!--
Thanks for contributing! Please read the PR guidelines first:
docs/en/development/pull-requests.md (English) | docs/zh/development/pull-requests.md (中文)

The PR title must follow Conventional Commits, e.g. "feat(api): add session listing".
It becomes the commit message on main after squash merge.
-->

## What

<!-- A short summary of the change. -->

## Why

<!-- The problem or motivation. Link the issue, e.g. "Closes #12". -->

Closes #

## How it was tested

<!-- Commands you ran, manual steps, screenshots or logs where helpful. -->

## Risks and notes for reviewers

<!-- Breaking changes, migrations, config changes, performance or security impact.
     Where copied or adapted code came from, if any. Write "None" if not applicable. -->

## Checklist

- [ ] PR title follows [Conventional Commits](https://www.conventionalcommits.org/)
- [ ] The change does one thing; unrelated changes are in separate PRs
- [ ] Tests added or updated, and `uv run pytest` passes
- [ ] `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy src` pass
- [ ] Docs updated (both `en` and `zh` if guidelines changed)
- [ ] `uv.lock` updated if dependencies changed
- [ ] No secrets, credentials or personal data included
- [ ] Breaking changes are marked with `!` and explained above
