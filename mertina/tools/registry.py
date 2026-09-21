# Ported from hermes-agent tools/registry.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Central registry for all tools: each tool file calls ``registry.register()``
at import to declare schema, handler, toolset membership and availability check;
``model_tools.py`` queries the registry instead of keeping parallel data structures.
Cycle-safe import chain: this module imports nothing from model_tools or tool files;
tools/*.py import it at module level; model_tools.py imports both; run_agent imports
model_tools."""

import ast
import importlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Cap on a tool error body; only trims runaway interpolated exceptions (static msgs are ~115 chars).
_MAX_TOOL_ERROR_CHARS = 2048
_TOOL_ERROR_TRUNCATION_MARKER = "… [truncated]"
# Logs keep more of the body than the model sees, but still a bounded amount.
_MAX_LOGGED_ERROR_CHARS = 8192


def _bound_error_text(text: str) -> str:
    """Bound an error body destined for model context; logs keep a longer prefix."""
    if len(text) <= _MAX_TOOL_ERROR_CHARS:
        return text
    logger.debug(
        "tool error body truncated for context (%d chars): %s",
        len(text),
        text[:_MAX_LOGGED_ERROR_CHARS],
    )
    return text[:_MAX_TOOL_ERROR_CHARS] + _TOOL_ERROR_TRUNCATION_MARKER


def _bound_json_error_result(result: str) -> str:
    """Trim an oversized ``error`` field in a JSON string result: handlers that
    ``json.dumps({"error": str(exc)})`` directly bypass ``tool_error``'s cap, so this runs
    at the dispatch boundary to stop unbounded errors stacking across retries."""
    if len(result) <= _MAX_TOOL_ERROR_CHARS or '"error"' not in result:
        return result
    try:
        payload = json.loads(result)
    except ValueError:
        return result
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, str) or len(error) <= _MAX_TOOL_ERROR_CHARS:
        return result
    payload["error"] = _bound_error_text(error)
    return json.dumps(payload, ensure_ascii=False)


def _is_registry_register_call(node: ast.AST) -> bool:
    """True when *node* is a ``registry.register(...)`` call expression."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "register"
        and isinstance(func.value, ast.Name)
        and func.value.id == "registry"
    )


def _module_registers_tools(module_path: Path) -> bool:
    """True when the module body (or a module-level ``for``) calls ``registry.register(...)``.
    Only module-body statements count, so helpers registering inside a function are skipped;
    a text prefilter avoids ``ast.parse`` for files lacking both words."""
    try:
        source = module_path.read_text(encoding="utf-8")
        if "registry" not in source or "register" not in source:
            return False
        tree = ast.parse(source, filename=str(module_path))
    except (OSError, SyntaxError):
        return False
    # Table-driven modules register several tools from one loop, still at import time.
    return any(
        _is_registry_register_call(stmt)
        or (isinstance(stmt, ast.For) and any(_is_registry_register_call(s) for s in stmt.body))
        for stmt in tree.body
    )


def _tool_module_candidates(tools_path: Path) -> list[Path]:
    """Flat ``tools/*.py`` modules plus the package entry point ``tools/<pkg>/tool.py``, in one
    sorted list. Only ``tool.py`` is scanned in a package, so every other file in it is a library
    by construction. Sorted after merging: ``register()`` lets a same-name, same-toolset duplicate
    overwrite silently, so the import order must not depend on file depth."""
    candidates = list(tools_path.glob("*.py")) + list(tools_path.glob("*/tool.py"))
    return sorted(candidates)


def discover_builtin_tools(tools_dir: Path | None = None) -> list[str]:
    """Import built-in self-registering tool modules and return their module names. Each
    candidate file is scanned with ``ast`` for a module-level ``registry.register()`` call."""
    tools_path = (
        Path(tools_dir) if tools_dir is not None else Path(__file__).resolve().parent
    ).resolve()
    module_names: list[str] = []
    for path in _tool_module_candidates(tools_path):
        if path.name in {"__init__.py", "registry.py", "mcp_tool.py"}:
            continue
        rel_parts = path.relative_to(tools_path).with_suffix("").parts
        if len(rel_parts) > 1 and not (path.parent / "__init__.py").exists():
            # setuptools' package finder drops a directory without __init__.py, so this tool would
            # register from a checkout and vanish from an installed wheel.
            logger.warning("Skipping %s: package %s has no __init__.py", path, path.parent.name)
            continue
        registers = _module_registers_tools(path)
        if registers:
            module_names.append(".".join(("mertina.tools", *rel_parts)))

    imported: list[str] = []
    for mod_name in module_names:
        try:
            importlib.import_module(mod_name)
            imported.append(mod_name)
        except Exception as e:
            logger.warning("Could not import tool module %s: %s", mod_name, e)
    return imported


@dataclass(eq=False, slots=True)
class ToolEntry:
    """Metadata for one registered tool."""

    name: str
    toolset: str
    schema: dict
    handler: Callable
    check_fn: Callable | None
    requires_env: list
    is_async: bool
    description: str
    emoji: str
    max_result_size_chars: int | float | None = None
    # Zero-arg callable whose dict is shallow-merged onto the schema at every get_definitions()
    # — for fields tracking runtime config (delegate_task's description reflects limits).
    dynamic_schema_overrides: Callable | None = None


def _fn_label(fn: Callable) -> object:
    return getattr(fn, "__qualname__", fn)


def _run_check_fn_uncached(fn: Callable) -> bool:
    """Run an availability check without cache/grace handling."""
    try:
        return bool(fn())
    except Exception:
        logger.warning(
            "check_fn %s raised; dependent tools will be unavailable this turn",
            _fn_label(fn),
            exc_info=True,
        )
    return False


def _check_fn_cached(fn: Callable) -> bool:
    """Return bool(fn()); the TTL cache is not ported, so every call probes."""
    return _run_check_fn_uncached(fn)


def _memo_check(fn: Callable, memo: dict[Callable, bool]) -> bool:
    """Per-pass memo: one probe per distinct check_fn."""
    if fn not in memo:
        memo[fn] = _check_fn_cached(fn)
    return memo[fn]


class ToolRegistry:
    """Singleton registry that collects tool schemas + handlers from tool files."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}  # built-in / process-global registrations
        self._toolset_checks: dict[str, Callable] = {}
        self._lock = threading.RLock()
        # Bumped on every mutation; get_tool_definitions memoizes against it.
        self._generation: int = 0

    @staticmethod
    def _grouped(entries: list[ToolEntry]) -> dict[str, list[ToolEntry]]:
        """``{toolset: entries}`` in first-appearance order."""
        groups: dict[str, list[ToolEntry]] = {}
        for entry in entries:
            groups.setdefault(entry.toolset, []).append(entry)
        return groups

    def _merged_tools(self) -> dict[str, ToolEntry]:
        """Return the registered tools."""
        return {**self._tools}

    def _snapshot_state(self) -> tuple[list[ToolEntry], dict[str, Callable]]:
        """Return a coherent snapshot of registry entries and toolset checks."""
        with self._lock:
            entries = list(self._merged_tools().values())
            checks = dict(self._toolset_checks)
            checks.update({e.toolset: e.check_fn for e in entries if e.check_fn is not None})
            return entries, checks

    def _snapshot_entries(self) -> list[ToolEntry]:
        return self._snapshot_state()[0]

    def _toolset_has_exposable_tools(self, toolset: str, entries: list[ToolEntry]) -> bool:
        """True when at least one tool in *toolset* would be exposed. Mirrors
        :meth:`get_definitions` per-tool filtering: mixed toolsets must not be gated
        by the first ``check_fn``."""
        memo: dict[Callable, bool] = {}
        members = (e for e in entries if e.toolset == toolset)
        return any(not e.check_fn or _memo_check(e.check_fn, memo) for e in members)

    def get_entry(self, name: str) -> ToolEntry | None:
        """Entry by name."""
        with self._lock:
            return self._merged_tools().get(name)

    # ---- Registration ------------------------------------------------

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable = None,
        requires_env: list = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        max_result_size_chars: int | float | None = None,
        dynamic_schema_overrides: Callable = None,
        override: bool = False,
    ):
        """Register a tool (called at import time by each tool file). ``override=True`` is an
        explicit opt-in for replacing a tool from another toolset; without it, cross-toolset
        shadowing is rejected."""
        # Reject malformed schemas at registration, not at request time: a non-dict
        # ``parameters`` (e.g. a list) serializes into every provider request and 400s the
        # whole turn far from the offending plugin. Failing here names the culprit instead.
        if not isinstance(schema, dict):
            raise ValueError(f"Tool {name!r}: schema must be a dict, got {type(schema).__name__}")
        params = schema.get("parameters")
        if params is not None and not isinstance(params, dict):
            raise ValueError(
                f"Tool {name!r}: schema['parameters'] must be an object (JSON Schema dict), "
                f"got {type(params).__name__}"
            )
        with self._lock:
            target = self._tools
            existing = self._tools.get(name)
            if existing and existing.toolset != toolset:
                if override:
                    # Explicit opt-in: INFO so the override is auditable.
                    logger.info(
                        "Tool '%s': toolset '%s' overriding existing toolset '%s' "
                        "(override=True opt-in)",
                        name,
                        toolset,
                        existing.toolset,
                    )
                else:
                    # Reject every cross-toolset shadow; same-toolset re-registration stays
                    # allowed.
                    logger.error(
                        "Tool registration REJECTED: '%s' (toolset '%s') would shadow existing "
                        "tool from toolset '%s'. Pass override=True to register() if the "
                        "replacement is intentional, or deregister the existing tool first.",
                        name,
                        toolset,
                        existing.toolset,
                    )
                    return
            target[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=check_fn,
                requires_env=requires_env or [],
                is_async=is_async,
                description=description or schema.get("description", ""),
                emoji=emoji,
                max_result_size_chars=max_result_size_chars,
                dynamic_schema_overrides=dynamic_schema_overrides,
            )
            if check_fn and toolset not in self._toolset_checks:
                self._toolset_checks[toolset] = check_fn
            self._generation += 1

    # ---- Schema retrieval --------------------------------------------

    def get_definitions(self, tool_names: set[str], quiet: bool = False) -> list[dict]:
        """OpenAI-format schemas for the requested tools whose ``check_fn`` passes (or is
        absent)."""
        result = []
        check_results: dict[Callable, bool] = {}
        entries_by_name = {entry.name: entry for entry in self._snapshot_entries()}
        for name in sorted(tool_names):
            entry = entries_by_name.get(name)
            if not entry:
                continue
            if entry.check_fn and not _memo_check(entry.check_fn, check_results):
                if not quiet:
                    logger.debug("Tool %s unavailable (check failed)", name)
                continue
            schema_with_name = {**entry.schema, "name": entry.name}
            # Runtime-dynamic overrides (e.g. delegate_task limits).
            if entry.dynamic_schema_overrides is not None:
                try:
                    overrides = entry.dynamic_schema_overrides()
                except Exception as exc:
                    overrides = None
                    logger.warning(
                        "dynamic_schema_overrides for tool %s raised %s; using static schema",
                        name,
                        exc,
                    )
                if isinstance(overrides, dict):
                    schema_with_name.update(overrides)
            result.append({"type": "function", "function": schema_with_name})
        return result

    # ---- Dispatch ----------------------------------------------------

    @staticmethod
    def _normalize_handler_result(name: str, result):
        """Results must be a string or the multimodal envelope; anything else becomes a
        string error so logging/hooks/budgeting/persistence never receive values they
        cannot slice or size."""
        if isinstance(result, str):
            return _bound_json_error_result(result)
        if (
            isinstance(result, dict)
            and result.get("_multimodal") is True
            and isinstance(result.get("content"), list)
        ):
            return result
        result_type = type(result).__name__
        logger.error("Tool %s handler returned unsupported result type: %s", name, result_type)
        return tool_error(
            f"Tool handler returned unsupported result type: {result_type}",
            error_type="tool_result_contract",
            tool=name,
            result_type=result_type,
        )

    def dispatch(self, name: str, args: dict, **kwargs) -> str | dict:
        """Execute a tool handler by name: async handlers bridged via ``_run_async()``,
        results normalized, every exception returned as ``{"error": ...}``."""
        entry = self.get_entry(name)
        if not entry:
            return tool_error(f"Unknown tool: {name}")
        try:
            if entry.is_async:
                from mertina.model_tools import _run_async

                result = _run_async(entry.handler(args, **kwargs))
            else:
                result = entry.handler(args, **kwargs)
            return self._normalize_handler_result(name, result)
        except Exception as e:
            # exc_info already renders the exception, so keep the message copy bounded.
            logger.exception("Tool %s dispatch error: %s", name, _bound_error_text(str(e)))
            # Sanitize so framing tokens/CDATA/fences in exception text aren't structural noise.
            raw = f"Tool execution failed: {type(e).__name__}: {e}"
            try:
                from mertina.model_tools import _sanitize_tool_error

                sanitized = _sanitize_tool_error(raw)
            except Exception:
                sanitized = raw  # defensive: never let the sanitizer block error propagation
            return tool_error(sanitized)

    # ---- Query helpers -----------------------------------------------

    def _attr(self, name: str, attr: str):
        return getattr(self.get_entry(name), attr, None)

    def get_all_tool_names(self) -> list[str]:
        return sorted(entry.name for entry in self._snapshot_entries())

    def get_toolset_for_tool(self, name: str) -> str | None:
        return self._attr(name, "toolset")

    def check_toolset_requirements(self) -> dict[str, bool]:
        entries = self._snapshot_entries()
        return {
            toolset: self._toolset_has_exposable_tools(toolset, entries)
            for toolset in sorted(self._grouped(entries))
        }


# Module-level singleton
registry = ToolRegistry()


# Tool handlers must return JSON strings; these replace the ubiquitous
# ``json.dumps({"error": msg}, ensure_ascii=False)`` boilerplate.


def tool_error(message, **extra) -> str:
    """``'{"error": "<message>", **extra}'`` — the error body is bounded so a raw
    exception can't bloat history across retries."""
    return json.dumps({"error": _bound_error_text(str(message)), **extra}, ensure_ascii=False)


def tool_result(data=None, **kwargs) -> str:
    """JSON-encode a dict positional arg *or* keyword arguments (not both)."""
    return json.dumps(data if data is not None else kwargs, ensure_ascii=False)
