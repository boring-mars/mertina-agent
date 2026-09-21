"""Check ported files against the upstream Hermes commit they name.

Every file under ``mertina/`` that opens with a ``# Ported from hermes-agent <path> @ <sha>`` header
is compared with that upstream file at that commit, after putting the upstream copy through the
mechanical changes that porting rule 2 allows (import roots, ``ruff --fix``, ``ruff format``).

Reported, for a person to read:
  * lines that are not in upstream: each one needs a reason from porting rule 2
  * with ``--cut-from <port commit>``: lines of a cut that are not plain deletions of that commit.
    ``prose`` is a comment or docstring, which may be rewritten to match the code. ``inline`` is
    what is left of a line after parts of it were deleted. ``rewrite`` is anything else and must
    have a row in the deviations table

Failures, which make the exit status 1:
  * kept definitions out of upstream order
  * import roots that were not rewritten, including module names passed as strings
  * a package ``__init__.py`` that upstream has but that was not ported

Usage::

    uv run python scripts/port_check.py [--upstream ../hermes-agent] [--cut-from REV] [paths...]
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

# Upstream top-level modules and packages that move under ``mertina`` (porting rule 1).
ROOTS = {
    *("acp_adapter", "agent", "cron", "gateway", "plugins", "providers", "tools", "tui_gateway"),
    *("batch_runner", "mcp_serve", "mini_swe_runner", "model_tools", "registration_lifecycle"),
    *("run_agent", "toolset_distributions", "toolsets", "trajectory_compressor", "utils"),
}
# Calls that take a module name as a string: lazy imports and logger names.
MODULE_NAME_CALLS = {"forward", "forward_static", "lazy_attr", "import_module", "getLogger"}
# How far past the last kept line an inline cut may reach for the rest of its words.
INLINE_WINDOW = 40


@dataclass
class Report:
    path: Path
    upstream: str
    sha: str
    new_lines: list[tuple[int, str]] = field(default_factory=list)
    cut_changes: list[tuple[int, str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def git_show(repo: Path, rev: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def map_module(name: str) -> str | None:
    """Rule 1 for a dotted module name, or None when it is not an upstream module."""
    first, dot, rest = name.partition(".")
    if first in ROOTS:
        return f"mertina.{name}"
    if first.startswith("hermes_"):
        return f"mertina.{first.removeprefix('hermes_')}{dot}{rest}"
    return None


def root_targets(source: str) -> list[tuple[int, str, str]]:
    """Import roots to rewrite, as ``(line, old text, new text)``.

    Only import statements and module names passed to MODULE_NAME_CALLS count; docstrings,
    comments and other strings are prose.
    """
    targets = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            new = map_module(node.module)
            if new:
                targets.append((node.lineno, f"from {node.module} ", f"from {new} "))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                new = map_module(alias.name)
                if new:
                    package, _, leaf = new.rpartition(".")
                    targets.append(
                        (node.lineno, f"import {alias.name}", f"from {package} import {leaf}")
                    )
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            arg = node.args[0]
            literal = arg.values[0] if isinstance(arg, ast.JoinedStr) and arg.values else arg
            if name.lstrip("_") in MODULE_NAME_CALLS and isinstance(literal, ast.Constant):
                module = re.split(r"[^\w.]", str(literal.value))[0].rstrip(".")
                new = map_module(module) if module else None
                if new:
                    targets.append((arg.lineno, f'"{module}', f'"{new}'))
    return targets


def rewrite_roots(source: str) -> str:
    lines = source.splitlines(keepends=True)
    for lineno, old, new in root_targets(source):
        lines[lineno - 1] = lines[lineno - 1].replace(old, new, 1)
    return "".join(lines)


def normalize(source: str) -> str:
    """Upstream source after rule 2's first two kinds of change."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "upstream.py"
        target.write_text(rewrite_roots(source), encoding="utf-8")
        ruff = [sys.executable, "-m", "ruff"]
        config = ["--config", str(REPO / "pyproject.toml"), "--quiet", str(target)]
        subprocess.run([*ruff, "check", "--fix-only", *config], capture_output=True, check=False)
        subprocess.run([*ruff, "format", *config], capture_output=True, check=False)
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


def prose_lines(source: str) -> set[int]:
    """Line numbers holding only a comment or part of a docstring."""
    prose = {n for n, line in enumerate(source.splitlines(), 1) if line.lstrip().startswith("#")}
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        first = body[0] if isinstance(body, list) and body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                prose.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return prose


def _words(text: str) -> list[str]:
    """Identifiers and punctuation, without the parentheses and commas ``ruff format`` moves."""
    return [w for w in re.findall(r"\w+|[^\w\s]", text) if w not in "(),"]


def classify_cut(before: str, after: str) -> list[tuple[int, str, str]]:
    """Lines of ``after`` that are not plain deletions of ``before``, as ``(line, text, kind)``.

    A cut keeps order, so each kept line is looked for after the previous one. A code line not
    found whole is ``inline`` when its words appear in order within the next INLINE_WINDOW lines
    of ``before``, so only deleting parts of them can produce it; otherwise it is a ``rewrite``.
    """
    old = [line.strip() for line in before.splitlines()]
    prose = prose_lines(after)
    position, found = 0, []
    for number, line in enumerate(after.splitlines(), 1):
        if not line.strip():
            continue
        if line.strip() in old[position:]:
            position = old.index(line.strip(), position) + 1
            continue
        window = [text for text in old[position : position + INLINE_WINDOW] if text[:1] != "#"]
        region = iter(_words(" ".join(window)))
        if number in prose:
            kind = "prose"
        elif all(word in region for word in _words(line)):
            kind = "inline"
        else:
            kind = "rewrite"
        found.append((number, line, kind))
    return found


def check_file(path: Path, upstream: Path, cut_from: str | None) -> Report | None:
    source = path.read_text(encoding="utf-8")
    match = HEADER.match(source)
    if match is None:
        return None
    upstream_path, sha = match.groups()
    report = Report(path, upstream_path, sha)
    original = git_show(upstream, sha, upstream_path)
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
    for lineno, old, _ in root_targets(source):
        report.failures.append(f"line {lineno}: import root not rewritten: {old.strip()}")
    before = git_show(REPO, cut_from, path.relative_to(REPO).as_posix()) if cut_from else None
    if before is not None:
        report.cut_changes = classify_cut(before, source)
    return report


def missing_package_inits(upstream: Path, sha: str) -> list[str]:
    """Package ``__init__.py`` files upstream has for a directory under ``mertina/`` but we lack.

    A hand-written placeholder init is the mistake this catches.
    """
    missing = []
    for directory in sorted({path.parent for path in PACKAGE.rglob("*.py")} - {PACKAGE}):
        first, _, rest = directory.relative_to(PACKAGE).as_posix().partition("/")
        first = "hermes_cli" if first == "cli" else first  # rule 1, backwards
        init = f"{first}/{rest}/__init__.py" if rest else f"{first}/__init__.py"
        ours = directory / "__init__.py"
        ported = ours.exists() and HEADER.match(ours.read_text("utf-8"))
        if git_show(upstream, sha, init) is not None and not ported:
            missing.append(f"{init} -> {ours.relative_to(REPO).as_posix()}")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--upstream", type=Path, default=REPO.parent / "hermes-agent")
    parser.add_argument("--cut-from", metavar="REV", help="classify lines that are not in REV")
    parser.add_argument("paths", nargs="*", type=Path, help="files to check (default: all)")
    args = parser.parse_args()

    files = [p.resolve() for p in args.paths] or sorted(PACKAGE.rglob("*.py"))
    reports = [r for r in (check_file(p, args.upstream, args.cut_from) for p in files) if r]
    failures = [f for r in reports for f in r.failures]
    for report in reports:
        print(f"{report.path.relative_to(REPO).as_posix()}  <-  {report.upstream}")
        for number, line in report.new_lines:
            print(f"  {number:4}: {line}")
        for number, line, kind in report.cut_changes:
            print(f"  {kind:7} {number:4}: {line}")
        for failure in report.failures:
            print(f"  FAIL {failure}")
    for sha in sorted({report.sha for report in reports}):
        for init in missing_package_inits(args.upstream, sha):
            print(f"FAIL package init not ported: {init}")
            failures.append(init)
    print(f"\n{len(reports)} ported files checked, {len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
