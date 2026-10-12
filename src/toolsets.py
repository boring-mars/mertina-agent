"""Toolset helpers: get/resolve/validate named tool groups (static TOOLSETS + registry-registered)."""

from typing import Dict, List, Any, Set, Optional, Tuple


# Shared tool list for CLI and all messaging platform toolsets (edit once, all
# platforms follow). Desktop GUI affordances are deliberately NOT here: they live
# in `desktop_ui`/`project`, enabled per desktop-sourced session by the GUI gateway
# (tui_gateway/server.py::_load_enabled_toolsets). HA, kanban and computer_use
# entries are further gated by their tools' check_fns.
_HERMES_CORE_TOOLS = ["web_search"]

# Webhook payloads are untrusted third-party content: no file/system execution.


def _ts(description, tools=(), includes=(), **extra):
    """One TOOLSETS entry (fresh lists per entry; extra keys such as posture pass through)."""
    return {"description": description, "tools": list(tools), "includes": list(includes), **extra}






# Coding posture: everything you reach for while pairing on code; drops messaging,
# tts, image_gen, home-assistant, cron, kanban and computer-use.

# Core toolset definitions: individual tools or references to other toolsets.
TOOLSETS = {
    "web": _ts("Web research and content extraction tools", ["web_search"]),
    "search": _ts("Web search only (no content extraction/scraping)", ["web_search"]),
}


def _registry():
    """Live tool registry, or None when tools.registry can't be imported."""
    try:
        from tools.registry import registry
        return registry
    except Exception:
        return None


def _registry_call(method: str, default):
    """registry.<method>() or *default* when the registry is unavailable or the call fails."""
    try:
        return getattr(_registry(), method)()
    except Exception:  # registry None (AttributeError) or the call failed
        return default


def _registry_generation() -> Tuple[int, int]:
    reg = _registry()
    return (id(reg), getattr(reg, "_generation", 0)) if reg is not None else (0, 0)


def get_toolset(name: str, *, include_registry: bool = True) -> Optional[Dict[str, Any]]:
    """Toolset definition, or None if unknown.

    include_registry=True merges plugin/overlay tools registered into this toolset
    and resolves registry-only (plugin/MCP) toolsets and aliases; False returns a
    copy of the static TOOLSETS entry only, so platform reverse-mapping is
    unaffected by registry additions.

    Args: name (str): Name of the toolset include_registry (bool): When True (default), merge in tools that
    plugins/overlays registered into this toolset via the registry. Platform reverse-mapping in
    ``_get_platform_tools`` uses False so that a tool registered into a toolset but absent from a platform's
    static composite does not drop the whole toolset from inference. See issue #49622.
    """
    toolset = TOOLSETS.get(name)
    if not include_registry:
        return {**toolset, "tools": list(toolset.get("tools", [])), "includes": list(toolset.get("includes", []))} if toolset else None

    registry = _registry()
    if registry is None:
        return toolset if toolset else None

    if toolset:
        merged_tools = set(toolset.get("tools", [])) | set(registry.get_tool_names_for_toolset(name))
        # An MCP server named like a built-in toolset ("homeassistant", "browser") registers a bare
        # alias to its `mcp-<name>` toolset; without this union the static entry shadows it and the
        # server's tools never reach the model even though discovery registered them.
        alias_target = registry.get_toolset_alias_target(name)
        if alias_target and alias_target != name:
            merged_tools |= set(registry.get_tool_names_for_toolset(alias_target))
        return {**toolset, "tools": sorted(merged_tools)}

    if name in _get_plugin_toolset_names():
        # Plugin toolset; shown as its MCP server alias when one exists.
        registry_toolset = name
        alias = _display_alias(name, _get_registry_toolset_aliases())
        description = f"MCP server '{alias}' tools" if alias else f"Plugin toolset: {name}"
    else:
        registry_toolset = registry.get_toolset_alias_target(name)
        if not registry_toolset:
            return None
        description = f"MCP server '{name}' tools"
    return {"description": description, "tools": registry.get_tool_names_for_toolset(registry_toolset), "includes": []}


def bundle_non_core_tools(toolset_name: str) -> Set[str]:
    """A bundle's tools minus _HERMES_CORE_TOOLS (one level of includes).

    Disabling a `core + extras` bundle must not strip the core tools every other
    toolset shares. One `includes` pass suffices (only hermes-gateway nests
    bundles). Unknown names: full resolution minus core.
    """
    core = set(_HERMES_CORE_TOOLS)
    ts_def = get_toolset(toolset_name)
    if not (ts_def and "tools" in ts_def):
        return set(resolve_toolset(toolset_name)) - core
    to_remove = set(ts_def["tools"])
    for inc_def in map(get_toolset, ts_def.get("includes", [])):
        if inc_def and "tools" in inc_def:
            to_remove.update(inc_def["tools"])
    return to_remove - core


# Memo keyed on (name, include_registry, id(registry), registry generation, profile scope);
# engages only at the public entry (visited is None). The scope is part of the key because a
# multiplexed process resolves ``mcp-<server>`` per profile overlay: without it profile B got
# profile A's tool names for a server B never connected (#106005).
_resolve_toolset_memo: Dict[Tuple[str, bool, int, int, str], List[str]] = {}


def _plugin_platform_bundle(name: str) -> List[str]:
    """Implicit `hermes-<platform>` bundle for a registered plugin platform: core
    tools plus whatever the plugin registered under the platform name. [] otherwise."""
    if not name.startswith("hermes-"):
        return []
    platform_name = name[len("hermes-"):]
    try:
        from gateway.platform_registry import platform_registry
        if not platform_registry.is_registered(platform_name):
            return []
    except Exception:
        return []
    tools = set(_HERMES_CORE_TOOLS)
    try:
        tools.update(e.name for e in _registry_call("get_all_entries", ()) if e.toolset == platform_name)
    except Exception:
        pass
    return list(tools)


def resolve_toolset(name: str, visited: Set[str] = None, *, include_registry: bool = True) -> List[str]:
    """Recursively resolve a toolset (and its includes) to a sorted tool-name list.
    include_registry=False resolves the static TOOLSETS view only.

    Args: name (str): Name of the toolset to resolve visited (Set[str]): Set of already visited toolsets
    (for cycle detection) include_registry (bool): When True (default), include tools that plugins/overlays
    registered into a toolset. Platform reverse-mapping uses False so a registry-added tool cannot drop the
    whole toolset from inference (see #49622 and ``_get_platform_tools``).
    """
    external_call = visited is None
    if external_call:
        memo_key = (name, include_registry, *_registry_generation(), _registry_call("current_scope_key", ""))
        cached = _resolve_toolset_memo.get(memo_key)
        if cached is not None:
            return list(cached)
        visited = set()

    # "all"/"*" span every toolset so new toolsets are included automatically.
    if name in {"all", "*"}:
        all_tools: Set[str] = set()
        for toolset_name in get_toolset_names():
            all_tools.update(resolve_toolset(toolset_name, visited.copy(), include_registry=include_registry))
        return sorted(all_tools)

    # Diamond include or cycle: [] silently — the tools are collected via another path.
    if name in visited:
        return []
    visited.add(name)

    toolset = get_toolset(name, include_registry=include_registry)
    if not toolset:
        return _plugin_platform_bundle(name) if include_registry else []

    tools = set(toolset.get("tools", []))
    for included_name in toolset.get("includes", []):
        tools.update(resolve_toolset(included_name, visited, include_registry=include_registry))

    result = sorted(tools)
    if external_call:
        if len(_resolve_toolset_memo) >= 256:  # stale-generation entries are never hit again
            _resolve_toolset_memo.clear()
        _resolve_toolset_memo[memo_key] = list(result)
    return result


def _get_plugin_toolset_names() -> Set[str]:
    """Registry toolset names absent from the static TOOLSETS dict."""
    return {n for n in _registry_call("get_registered_toolset_names", ()) if n not in TOOLSETS}


def _get_registry_toolset_aliases() -> Dict[str, str]:
    return _registry_call("get_registered_toolset_aliases", {})


def _display_alias(ts_name: str, aliases: Dict[str, str]) -> Optional[str]:
    """First non-static alias pointing at *ts_name*, or None."""
    return next((a for a, canonical in aliases.items() if canonical == ts_name and a not in TOOLSETS), None)


def _plugin_display_names() -> List[str]:
    """Plugin toolset names, shown under their first non-static alias when one exists."""
    aliases = _get_registry_toolset_aliases()
    return [_display_alias(n, aliases) or n for n in _get_plugin_toolset_names()]


def get_all_toolsets() -> Dict[str, Dict[str, Any]]:
    """All toolset definitions: static plus plugin-registered."""
    result = dict(TOOLSETS)
    aliases = _get_registry_toolset_aliases()
    for display_name in _plugin_display_names():
        toolset = None if display_name in result else get_toolset(display_name)
        if toolset:
            result[display_name] = toolset
    # Static names an MCP server also aliases show the merged view get_toolset() resolves.
    for name in TOOLSETS.keys() & aliases.keys():
        result[name] = get_toolset(name) or result[name]
    return result


def get_toolset_names() -> List[str]:
    """Sorted names of all toolsets (static + plugin), excluding aliases."""
    return sorted(set(TOOLSETS.keys()) | set(_plugin_display_names()))


def validate_toolset(name: str) -> bool:
    return (name in {"all", "*"} or name in TOOLSETS
            or name in _get_plugin_toolset_names() or name in _get_registry_toolset_aliases())


def create_custom_toolset(name: str, description: str, tools: List[str] = None, includes: List[str] = None) -> None:
    """Register a runtime toolset in TOOLSETS."""
    TOOLSETS[name] = _ts(description, tools or [], includes or [])


def get_toolset_info(name: str) -> Dict[str, Any]:
    """Toolset definition plus its resolved tools, or None if unknown."""
    toolset = get_toolset(name)
    if not toolset:
        return None
    resolved_tools = resolve_toolset(name)
    return {
        "name": name, "description": toolset["description"],
        "direct_tools": toolset["tools"], "includes": toolset["includes"],
        "resolved_tools": resolved_tools, "tool_count": len(resolved_tools),
        "is_composite": bool(toolset["includes"]),
    }


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

def resolve_multiple_toolsets(toolset_names: List[str]) -> List[str]:
    """
    Resolve multiple toolsets and combine their tools.

    Args:
        toolset_names (List[str]): List of toolset names to resolve

    Returns:
        List[str]: Combined list of all tool names (deduplicated)
    """
    all_tools = set()

    for name in toolset_names:
        tools = resolve_toolset(name)
        all_tools.update(tools)

    return sorted(all_tools)
# ---- END PLUGIN-COMPAT ----
