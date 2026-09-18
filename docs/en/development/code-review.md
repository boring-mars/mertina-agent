# Code Review

**English** | [中文](../../zh/development/code-review.md)

Code review keeps `main` healthy and spreads knowledge of the code across the team.
Anyone may review. An approval from a collaborator is what counts toward merging.

## Goals

In order of importance:

1. **Correctness**: does it do what it claims, including edge cases and failures?
2. **Security**: does it put users, their data or their credentials at risk?
3. **Design**: does it fit the architecture? Is it the simplest thing that works?
4. **Maintainability**: will the next person understand and safely change it?
5. **Consistency**: does it follow these guidelines and the surrounding code?

Formatting, import order and lint problems are left to the tools. Reviewers shouldn't spend time on them.

## The standard for approval

Approve a PR once it **clearly improves the overall health of the code**, even if it isn't perfect.
There is no perfect code, only better code. Don't hold a PR back for polish that could be a `nit:` or a follow-up.

- Technical facts and data win over personal preference
- On style, these guidelines and the tools are the authority. Anything they don't cover is the author's choice,
  as long as it is consistent with the surrounding code
- A PR must not make the code worse. If it adds complexity, it has to pay for it

## Who reviews

- GitHub requests reviews automatically based on `.github/CODEOWNERS` (once it is added)
- The author may also request specific reviewers
- Authors cannot approve their own PRs
- For changes to security-sensitive areas (authentication, permissions, tenant isolation, secrets handling),
  get a review from someone familiar with that area

## Response time

- Aim to give a first response within **2 working days**. It can be a full review, or just "I'll look at this on Friday"
- If you can't review, say so, so the author can find someone else
- Authors: if you get no response after a week, ping the reviewers in the PR

## Checklist for reviewers

**Correctness**
- [ ] Does the change solve the problem described in the PR or issue?
- [ ] Are edge cases handled: empty input, `None`, timeouts, retries, concurrent requests?
- [ ] Are errors handled explicitly, not swallowed? Are error messages useful?

**Security** (see [Security](security.md))
- [ ] No secrets or credentials in code, tests, logs or error messages
- [ ] User input is validated. No injection risk (SQL, shell, path traversal, prompt injection into tool calls)
- [ ] Authentication and authorization are checked, and one tenant cannot reach another tenant's data
- [ ] New dependencies are maintained, have a compatible license, and are really needed

**Design**
- [ ] Is the code in the right place? Does it duplicate something that already exists?
- [ ] Are public APIs, configuration keys and storage formats well named? They are hard to change later
- [ ] Is it a breaking change? If so, is it marked and is there a migration path?

**Tests and docs**
- [ ] Do the tests cover the new behavior and would they fail without the change?
- [ ] Are docs, config examples and the changelog-relevant PR title updated?

**Operations** (it is a cloud service)
- [ ] Is there enough logging to debug problems in production, without logging sensitive data?
- [ ] Are timeouts and resource limits set for external calls?
- [ ] Are database or storage migrations backward compatible and safe to roll back?

## Writing review comments

- Be specific and explain why: "This will fail when `items` is empty because…" rather than "this is wrong"
- Ask questions when you are unsure: "What happens if the upload times out here?"
- Suggest concrete changes. GitHub's **suggestion** blocks let the author apply them in one click
- Comment on the code, not the person. Say "this function", not "you"
- Point out what is done well, too
- Label how important each comment is, so the author knows what blocks merging:

| Prefix | Meaning |
|---|---|
| *(none)* | Must be addressed before merge |
| `nit:` | Minor or stylistic. The author may ignore it |
| `suggestion:` | Worth considering, not required |
| `question:` | Please explain. May or may not lead to a change |
| `follow-up:` | Valid, but better done in a separate PR or issue |

## Review outcomes

- **Approve**: good to merge once CI passes. Leaving `nit:` comments with an approval is fine
- **Comment**: feedback without a decision. Use it when you only reviewed part of the PR
- **Request changes**: must be addressed before merge. Explain clearly what is needed.
  Come back and re-review promptly once the author responds

If a PR is going in the wrong direction, say so early and kindly, before line-by-line comments.
Suggest discussing it in the issue.

## For authors

- Review your own diff before requesting review. Many issues are easy to spot there
- Don't take comments personally. Everyone's code gets reviewed
- If you disagree, explain your reasoning. If you still can't agree, ask a maintainer to decide
  (see [Governance](governance.md#how-decisions-are-made))
- If a discussion goes back and forth more than twice, move it to a call or an issue
