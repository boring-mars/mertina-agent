"""Check ported files against the upstream Hermes commit they name.

Every file under ``mertina/`` that opens with a ``# Ported from hermes-agent <path> @ <sha>`` header
is compared with that upstream file at that commit, after putting the upstream copy through the
mechanical changes that porting rule 2 allows (import roots, ``ruff --fix``, ``ruff format``).

Reported, for a person to read:
  * lines that are not in upstream: cuts leave none, so each one is a rewrite to justify

Failures, which make the exit status 1:
  * kept definitions out of upstream order
  * import roots that were not rewritten, including module paths inside strings
  * a package ``__init__.py`` that upstream has but that was not ported

Usage::

    uv run python scripts/port_check.py [--upstream ../hermes-agent] [paths...]
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "mertina"
HEADER = re.compile(r"^# (?:Ported|Derived) from hermes-agent (\S+) @ ([0-9a-f]+)")

_ROOTS = r"agent|tools|providers|plugins|utils|run_agent|model_tools"
_CALLS = r"_?forward(?:_static)?|_?lazy_attr|import_module"
# Porting rule 1 applied to source text: upstream import roots move under ``mertina``,
# and a ``hermes_`` prefix is stripped rather than replaced.
REWRITES = [
    (re.compile(rf"^(\s*)from ({_ROOTS})([. ])", re.M), r"\1from mertina.\2\3"),
    (re.compile(r"^(\s*)from hermes_([a-z_]+)([. ])", re.M), r"\1from mertina.\2\3"),
    (
        re.compile(r"^(\s*)import (run_agent|model_tools)(\s|$)", re.M),
        r"\1from mertina import \2\3",
    ),
    (re.compile(r"^(\s*)import hermes_([a-z_]+)(\s|$)", re.M), r"\1from mertina import \2\3"),
    (re.compile(r"^(\s*)from agent import ", re.M), r"\1from mertina.agent import "),
    (re.compile(rf"(({_CALLS})\(f?)\"({_ROOTS})([.\"])"), r'\1"mertina.\3\4'),
    (re.compile(rf"(({_CALLS})\(f?)\"hermes_([a-z_]+)([.\"])"), r'\1"mertina.\3\4'),
]
_UPSTREAM_MODULE = rf"(?:{_ROOTS}|hermes_[a-z_]+)"
UNREWRITTEN = re.compile(
    rf"^\s*(?:from|import) {_UPSTREAM_MODULE}\b|(?:{_CALLS})\(f?\"{_UPSTREAM_MODULE}[.\"]",
    re.M,
)


@dataclass
class Report:
    path: Path
    upstream: str
    sha: str
    new_lines: list[tuple[int, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def rewrite_roots(source: str) -> str:
    for pattern, replacement in REWRITES:
        source = pattern.sub(replacement, source)
    return source


def upstream_source(upstream: Path, sha: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(upstream), "show", f"{sha}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def normalize(source: str) -> str:
    """Upstream source after rule 2's first two kinds of change."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "upstream.py"
        target.write_text(rewrite_roots(source), encoding="utf-8")
        config = str(REPO / "pyproject.toml")
        ruff = [sys.executable, "-m", "ruff"]
        subprocess.run(
            [*ruff, "check", "--config", config, "--fix-only", "--quiet", str(target)],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            [*ruff, "format", "--config", config, "--quiet", str(target)],
            capture_output=True,
            check=False,
        )
        return target.read_text(encoding="utf-8")


def definitions(source: str) -> list[str]:
    """Top-level names in order: functions, classes (with their methods) and assignments."""
    names: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
            if isinstance(node, ast.ClassDef):
                names += [
                    f"{node.name}.{item.name}"
                    for item in node.body
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                ]
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name):
                names.append(target.id)
    return names


def in_order(ours: list[str], theirs: list[str]) -> bool:
    remaining = iter(theirs)
    return all(any(name == candidate for candidate in remaining) for name in ours if name in theirs)


def check_file(path: Path, upstream: Path) -> Report | None:
    source = path.read_text(encoding="utf-8")
    match = HEADER.match(source)
    if match is None:
        return None
    upstream_path, sha = match.groups()
    report = Report(path, upstream_path, sha)
    original = upstream_source(upstream, sha, upstream_path)
    if original is None:
        report.failures.append(f"{upstream_path} does not exist at {sha}")
        return report
    theirs = normalize(original)
    their_lines = {line.strip() for line in theirs.splitlines()}
    for number, line in enumerate(source.splitlines(), 1):
        if line.strip() and line.strip() not in their_lines and not line.startswith("# "):
            report.new_lines.append((number, line))
    if not in_order(definitions(source), definitions(theirs)):
        report.failures.append("kept definitions are not in upstream order")
    for hit in UNREWRITTEN.finditer(source):
        line_number = source.count("\n", 0, hit.start()) + 1
        report.failures.append(
            f"line {line_number}: import root not rewritten: {hit.group().strip()}"
        )
    return report


def missing_package_inits(ported: list[Report], upstream: Path, sha: str) -> list[str]:
    """Package ``__init__.py`` files upstream has but we did not port.

    Covers every package directory that exists under ``mertina/`` (a placeholder init written
    by hand is the mistake this catches) and every directory holding a ported file.
    """
    directories = {
        init.parent.relative_to(PACKAGE).as_posix()
        for init in PACKAGE.rglob("__init__.py")
        if init.parent != PACKAGE
    }
    for report in ported:
        parent = Path(report.upstream).parent
        while parent != Path():
            directories.add(upstream_to_ours(parent.as_posix()))
            parent = parent.parent
    missing = []
    for directory in sorted(directories):
        ours = PACKAGE / directory / "__init__.py"
        init = f"{ours_to_upstream(directory)}/__init__.py"
        has_upstream = upstream_source(upstream, sha, init) is not None
        if has_upstream and not (ours.exists() and HEADER.match(ours.read_text("utf-8"))):
            missing.append(f"{init} -> {ours.relative_to(REPO).as_posix()}")
    return missing


def upstream_to_ours(upstream_path: str) -> str:
    """Rule 1: the upstream path under ``mertina/``, with a ``hermes_`` prefix stripped."""
    first, _, rest = upstream_path.partition("/")
    first = first.removeprefix("hermes_")
    return f"{first}/{rest}" if rest else first


def ours_to_upstream(path: str) -> str:
    """The inverse of ``upstream_to_ours`` for the one prefixed top-level package we map."""
    first, _, rest = path.partition("/")
    first = "hermes_cli" if first == "cli" else first
    return f"{first}/{rest}" if rest else first


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--upstream", type=Path, default=REPO.parent / "hermes-agent")
    parser.add_argument("paths", nargs="*", type=Path, help="files to check (default: all)")
    args = parser.parse_args()

    files = [p.resolve() for p in args.paths] or sorted(PACKAGE.rglob("*.py"))
    reports = [r for r in (check_file(p, args.upstream) for p in files) if r is not None]
    failed = False
    for report in reports:
        print(f"{report.path.relative_to(REPO).as_posix()}  <-  {report.upstream}")
        for number, line in report.new_lines:
            print(f"  {number:4}: {line}")
        for failure in report.failures:
            print(f"  FAIL {failure}")
            failed = True
    for sha in sorted({report.sha for report in reports}):
        for init in missing_package_inits(reports, args.upstream, sha):
            print(f"FAIL package init not ported: {init}")
            failed = True
    print(f"\n{len(reports)} ported files checked, {'failures found' if failed else 'no failures'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
