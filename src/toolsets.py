"""The v0.1 toolset contains only gateway web search."""

from typing import Optional

_HERMES_CORE_TOOLS = ["web_search"]
TOOLSETS = {
    "web": {"description": "Web search", "tools": ["web_search"], "includes": []},
    "search": {"description": "Web search", "tools": ["web_search"], "includes": []},
}


def get_toolset(name: str, **_kwargs) -> Optional[dict]:
    if name in {"all", "*"}:
        return {"description": "All tools", "tools": list(_HERMES_CORE_TOOLS), "includes": []}
    value = TOOLSETS.get(name)
    return {**value, "tools": list(value["tools"]), "includes": []} if value else None


def resolve_toolset(name: str, *_args, **_kwargs) -> list[str]:
    value = get_toolset(name)
    return list(value["tools"]) if value else []


def validate_toolset(name: str) -> bool:
    return name in TOOLSETS or name in {"all", "*"}


def bundle_non_core_tools(_name: str) -> set[str]:
    return set()


def get_all_toolsets() -> dict[str, dict]:
    return {name: get_toolset(name) for name in TOOLSETS}


def get_toolset_names() -> list[str]:
    return sorted(TOOLSETS)
