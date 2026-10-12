#!/usr/bin/env python3
"""Hermes ``web_search`` tool backed by the configured model gateway."""

import json

from agent.web_search_registry import get_active_search_provider
from tools.registry import registry, tool_error


def web_search_tool(query: str, limit: int = 5) -> str:
    """Search through the gateway provider and return the Hermes result shape."""
    try:
        limit = min(max(int(limit), 1), 100)
    except (TypeError, ValueError):
        limit = 5

    try:
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return tool_error("Interrupted", success=False)
        provider = get_active_search_provider()
        if provider is None or not provider.is_available():
            return tool_error("No web search provider configured", success=False)
        return json.dumps(provider.search(query, limit), indent=2, ensure_ascii=False)
    except Exception as exc:
        return tool_error(f"Error searching web: {exc}", success=False)


def check_web_api_key() -> bool:
    provider = get_active_search_provider()
    return bool(provider is not None and provider.is_available())


WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for current information and return titles, URLs, and descriptions.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The web search query."},
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return.",
                "minimum": 1,
                "maximum": 100,
                "default": 5,
            },
        },
        "required": ["query"],
    },
}


registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **_kwargs: web_search_tool(
        args.get("query", ""), limit=args.get("limit", 5)
    ),
    check_fn=check_web_api_key,
    requires_env=[],
    emoji="🔍",
    max_result_size_chars=100_000,
)
