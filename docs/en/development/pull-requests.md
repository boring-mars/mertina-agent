# Pull Requests

**English** | [中文](../../zh/development/pull-requests.md)

Every change reaches `main` through a pull request (PR), including changes from maintainers.

## Before opening a PR

- [ ] The branch is based on the latest `main` and follows the [naming rules](branching.md#branch-naming)
- [ ] The change does one thing. Unrelated fixes go in separate PRs
- [ ] Tests pass locally: `uv run pytest`
- [ ] Lint, format and type checks pass: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`
- [ ] New behavior has tests. Fixed bugs have a regression test
- [ ] Documentation is updated if behavior, configuration or APIs changed
- [ ] If dependencies changed, `uv.lock` is updated and committed
- [ ] No secrets, credentials, personal data or large generated files are included

## Title

The PR title becomes the commit message on `main`, so it must follow the [Commit Convention](commit-convention.md):

```
feat(storage): add S3 session store
fix(api): return 404 instead of 500 for unknown session
docs: add development guidelines
```

CI rejects PRs whose title does not match the format.

## Description

Fill in the PR template. A good description answers:

- **What** changes, briefly
- **Why**: the problem or the motivation. Link the issue: `Closes #12`
- **How**: the approach, and alternatives you rejected, if that isn't obvious
- **How it was tested**: commands you ran, manual steps, screenshots or logs where helpful
- **Risks**: breaking changes, migrations, config changes, performance or security impact
- **Attribution**: where copied or adapted code came from, if any

Write the description for a reviewer who has not read the issue. If it takes long to explain, the PR is probably too big.

## Size

Small PRs are reviewed faster and more thoroughly.

- Aim for **under ~400 lines** of meaningful change (generated files and lockfiles don't count)
- Split large work into a sequence of PRs that each keep `main` working, for example
  interfaces first, then the implementation, then wiring it up
- Put pure moves and renames in their own PR, separate from logic changes
- Unfinished features can be merged behind a configuration flag that is off by default

## Draft PRs

Open a **draft PR** when you want early feedback on the direction or want CI to run, but the work isn't finished.
Mark it **Ready for review** when it is. Reviewers normally don't review drafts in detail unless asked.

## During review

- Push new commits to address feedback. Avoid force-pushing while a review is in progress,
  because it makes it hard for reviewers to see what changed. Rebase at the end if needed
- Reply to every comment: either change the code, or explain why not. Don't silently ignore a comment
- The **reviewer** who opened a conversation resolves it, unless they told you to resolve it yourself
- Re-request review (the 🔄 button) when you are done with a round
- Pushing new commits dismisses earlier approvals. This is intentional

## Merging

A PR can be merged when:

- [ ] At least 1 approval from someone other than the author
- [ ] All required CI checks are green
- [ ] All conversations are resolved
- [ ] The branch is up to date with `main`

Then:

- Merge with **Squash and merge**. It is the only merge method enabled
- Check the final commit message GitHub proposes. Keep the PR title as the subject, and tidy the body
  (drop noise like "fix typo", "address review") so that it is a useful summary
- Who merges: normally the author, if they have write access; otherwise the approving collaborator
- The branch is deleted automatically after merge

## Stale PRs

- PRs with no activity from the author for **30 days** get a reminder
- If there is still no response **14 days** later, the PR may be closed. It can be reopened at any time
- A maintainer may take over an abandoned PR that is nearly finished. The original author keeps credit
  through a `Co-authored-by:` footer

## Reverting

If a merged PR breaks `main`, the first priority is to get `main` green again:

- Use the **Revert** button on the merged PR. It creates a revert PR, which then follows the normal review flow
- Fix forward in a new PR once the problem is understood
