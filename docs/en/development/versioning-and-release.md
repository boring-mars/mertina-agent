# Versioning and Release

**English** | [中文](../../zh/development/versioning-and-release.md)

## Semantic Versioning

Versions follow [Semantic Versioning 2.0](https://semver.org/): `MAJOR.MINOR.PATCH`

| Part | Bump when | Example |
|---|---|---|
| `MAJOR` | A breaking change: users must change something to upgrade | `1.4.2` → `2.0.0` |
| `MINOR` | A backward-compatible new feature | `1.4.2` → `1.5.0` |
| `PATCH` | A backward-compatible bug fix | `1.4.2` → `1.4.3` |

What counts as the **public interface**, where a change can be breaking:

- The Python API exported from `mertina`
- The HTTP API
- CLI commands and options
- Configuration keys and environment variables
- Storage formats and database schemas (an upgrade that needs a manual migration is breaking)

### Before 1.0

The project starts at `0.x.y`. While the major version is 0, the API is not considered stable:

- Breaking changes bump **MINOR**: `0.3.1` → `0.4.0`
- Features and fixes bump **PATCH**: `0.3.1` → `0.3.2`
- Breaking changes are still marked in commits and listed clearly in the release notes

`1.0.0` is released when the public interface is stable enough that we commit to keeping it compatible.

### Pre-releases

Pre-releases use PEP 440 suffixes, which Python packaging understands:
`1.0.0a1` (alpha), `1.0.0b1` (beta), `1.0.0rc1` (release candidate).
Git tags add the `v` prefix: `v1.0.0rc1`.

## Where the version lives

- **Git tags are the single source of truth.** Every release is an annotated tag `vX.Y.Z` on `main`
- The version in `pyproject.toml` is maintained by the release tooling, not edited by hand in feature PRs
- Tags are never moved or deleted after they are pushed. If a release is broken, release a new version

## Changelog

- `CHANGELOG.md` at the repository root follows the [Keep a Changelog](https://keepachangelog.com/) style
- It is generated from the Conventional Commit titles of merged PRs, which is why PR titles matter
- Types that appear in the changelog: `feat`, `fix`, `perf`, `revert` and anything marked breaking.
  Types like `docs`, `test`, `ci` and `chore` are omitted
- Breaking changes are listed first with migration instructions

## Release process

Releases are automated with [release-please](https://github.com/googleapis/release-please):

```
merge PRs into main
      │
      ▼
release-please opens / updates a "Release PR"
(bumps the version in pyproject.toml, updates CHANGELOG.md)
      │
      ▼  a maintainer reviews and merges the Release PR
      │
      ▼
tag vX.Y.Z + GitHub Release are created
      │
      ▼
CI builds with `uv build` and publishes to PyPI
(and builds and pushes the container image)
```

1. As PRs are merged, release-please keeps a Release PR up to date, computing the next version from the commit types
2. When the maintainer decides to release, they check the Release PR: the version is right, the changelog reads well,
   breaking changes have migration notes. They edit the changelog in the Release PR if needed
3. Merging the Release PR creates the tag and the GitHub Release
4. The tag triggers the publish workflow:
   - `uv build` produces the sdist and wheel
   - The package is published to PyPI with [Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
     so no API token is stored in the repository
   - The container image is built and pushed, tagged with the version
5. The maintainer checks that the package installs (`uvx mertina-agent --version`) and that the image runs

Only the maintainer merges Release PRs.

## Release cadence

There is no fixed schedule. Release when there is something worth releasing, typically:

- Soon after an important fix, especially a security fix
- Every few weeks while features are being added

## Patch releases for older versions

Normally only the latest release gets fixes. Users should upgrade.
If an older major version has to be maintained, see [Maintenance branches](branching.md#maintenance-branches-future).

## Deprecation

Before removing or changing a public interface:

1. Mark it deprecated in a minor release: emit a `DeprecationWarning`, document it, and list it in the changelog along with its replacement
2. Keep it working for at least one more minor release
3. Remove it in the next major release (or the next minor release before 1.0)
