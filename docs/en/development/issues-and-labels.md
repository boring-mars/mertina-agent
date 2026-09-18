# Issues and Labels

**English** | [中文](../../zh/development/issues-and-labels.md)

## Before opening an issue

- Search existing issues, including closed ones
- **Security vulnerabilities must not be reported in public issues.** See [Security](security.md#reporting-a-vulnerability)
- For general questions and ideas that aren't concrete yet, use Discussions (if enabled) rather than issues

## Bug reports

A good bug report lets someone else reproduce the problem. Include:

- **Version**: Mertina Agent version or commit, Python version, OS, and how it is deployed
- **Steps to reproduce**: the smallest set of steps or code that shows the problem
- **Expected behavior** and **actual behavior**
- **Logs and error messages**, as text rather than screenshots, with **secrets, tokens and personal data removed**
- Anything you already tried

## Feature requests

Describe the **problem** before the solution:

- What are you trying to do, and why is it hard today?
- What would you like to happen?
- What alternatives have you considered?
- Are you willing to implement it?

A feature request is a proposal. It needs agreement (the `accepted` label) before a large implementation PR.

## Issue lifecycle

```
new ──► triage ──► needs-info ──► ...
            │
            ├──► accepted ──► in progress (assigned / linked PR) ──► closed by PR
            │
            └──► closed (duplicate / wontfix / invalid)
```

- **Triage**: a collaborator labels new issues with a type and, if possible, a priority. Aim to triage within a week
- **Needs info**: if the reporter doesn't respond within 14 days, the issue may be closed. It can be reopened with the missing information
- **Assignment**: comment to claim an issue. It is then assigned to you. If there is no progress for 30 days,
  it may be unassigned so someone else can pick it up
- **Closing**: issues are closed automatically by the PR that fixes them (`Closes #12`). Closing for any other reason comes with a short explanation

## Labels

Labels are grouped below by purpose. Keep the set small. New labels need a reason.

**Type** (one per issue)

| Label | Meaning |
|---|---|
| `bug` | Something doesn't work as documented |
| `enhancement` | New feature or improvement |
| `documentation` | Documentation only |
| `question` | A question, not a change request |
| `security` | Security hardening (not undisclosed vulnerabilities) |

**Status**

| Label | Meaning |
|---|---|
| `needs-triage` | Not yet looked at by a collaborator |
| `needs-info` | Waiting for the reporter |
| `needs-discussion` | Approach not agreed yet |
| `accepted` | Agreed to be done. Ready to be picked up |
| `blocked` | Waiting on something else |

**Priority** (optional)

| Label | Meaning |
|---|---|
| `priority: high` | Serious bug, data loss, security issue, or blocks many users |
| `priority: low` | Nice to have |

**Contributors**

| Label | Meaning |
|---|---|
| `good first issue` | Small, well-defined, a good first contribution. Include pointers to the relevant code |
| `help wanted` | Maintainers would welcome a contribution |

**Resolution**

| Label | Meaning |
|---|---|
| `duplicate` | Already reported. Link the original |
| `wontfix` | Won't be done. Explain why |
| `invalid` | Not a bug, or not enough to act on |

**Other**

| Label | Meaning |
|---|---|
| `breaking` | Involves a breaking change |
| `dependencies` | Dependency updates (used by Dependabot / Renovate) |
| `flaky-test` | A test that fails intermittently |

## Roadmap

Planned work is tracked in a GitHub Project board and GitHub milestones. Issues in a milestone are planned for that release.
Being in the roadmap doesn't mean someone is working on it. Check the assignee.
