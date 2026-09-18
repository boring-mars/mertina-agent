# Commit Convention

**English** | [中文](../../zh/development/commit-convention.md)

The project follows [Conventional Commits 1.0](https://www.conventionalcommits.org/en/v1.0.0/).
Structured messages make the history readable and let tooling generate the changelog and the next version number automatically.

## Where the convention applies

Because every PR is **squash merged**, each PR becomes exactly one commit on `main`, and **the PR title becomes that commit's subject line**. So:

- **PR titles must follow the convention.** CI checks this.
- Commit messages inside your feature branch are not checked. Following the convention there is still good practice,
  because it helps reviewers and makes it easy to split work later.

## Format

```
<type>(<optional scope>)<optional !>: <description>

<optional body>

<optional footer(s)>
```

Example:

```
feat(api): add endpoint for listing sessions

Sessions can now be listed with pagination. The default page size is 20.

Closes #37
```

## Types

| Type | Meaning | Effect on version (after 1.0) |
|---|---|---|
| `feat` | A new feature | minor |
| `fix` | A bug fix | patch |
| `perf` | A performance improvement | patch |
| `docs` | Documentation only | none |
| `refactor` | Code change that neither fixes a bug nor adds a feature | none |
| `test` | Adding or fixing tests | none |
| `build` | Build system, packaging or dependencies | none |
| `ci` | CI configuration | none |
| `style` | Formatting only, no change in meaning | none |
| `chore` | Other changes that don't touch source or tests | none |
| `revert` | Reverts a previous commit | depends |

A breaking change in any type means a major version bump (see [Breaking changes](#breaking-changes)).

## Scope

The scope is optional and names the part of the codebase affected, in lowercase, for example
`api`, `cli`, `storage`, `auth`, `deploy`, `docs`. Use one scope per commit. If a change spans many areas, leave the scope out.
The list of scopes will grow with the codebase. Prefer reusing an existing scope over inventing a new one.

## Description

- Written in **English**, in the **imperative mood**: "add", not "added" or "adds"
- Starts with a lowercase letter and has no trailing period
- Keep the whole subject line under about 72 characters
- Says *what* changes. The body explains *why*

| ✅ Good | ❌ Bad |
|---|---|
| `fix(storage): retry S3 upload on throttling` | `fixed bug` |
| `feat(cli): add --region option` | `Feat: Added region option.` |
| `docs: explain local deployment with docker compose` | `update docs` |
| `refactor(agent): extract tool registry` | `refactor stuff and fix tests and bump deps` |

## Body

- Separated from the subject by a blank line, wrapped at about 72 characters
- Explains the motivation and contrasts with the previous behavior
- Can be written in English or Chinese, but English is preferred so every contributor can read it

## Footers

- Link issues: `Closes #12`, `Fixes #12`, `Refs #12`
- Credit co-authors: `Co-authored-by: Name <email>`
- Breaking changes: `BREAKING CHANGE: <description>`

## Breaking changes

A change is breaking if existing users must change something to keep working: a removed or renamed API, a changed
configuration key, an incompatible storage format, a required migration, and so on.

Mark it in either or both of these ways:

```
feat(api)!: require API key for all endpoints

BREAKING CHANGE: anonymous access is no longer allowed. Set MERTINA_API_KEY
before upgrading.
```

Always explain in the footer what users must do to migrate.

## Reverts

```
revert: feat(api): add endpoint for listing sessions

This reverts commit 1a2b3c4. The endpoint leaked sessions across tenants.
```
