"""The ``web_search`` tool: schema, handler and registration.

Copied from Hermes tools/web_tools.py (``web_search_tool``, ``WEB_SEARCH_SCHEMA``,
``check_web_api_key`` and the ``registry.register`` call) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

Hermes resolves the backend from its config at every call and supports many
providers, extraction, result caching and a rescue ring. Mertina binds one
provider when the tool is registered: importing this module registers
``web_search`` with the default ddgs provider, and
:func:`configure_web_search` re-registers it from validated settings.
"""

import json
import logging

from mertina_agent.agent.interrupt import is_interrupted
from mertina_agent.agent.transports.types import FunctionDefinition, JsonObject
from mertina_agent.agent.web_search_provider import WebSearchProvider
from mertina_agent.config import Settings
from mertina_agent.plugins.web.ddgs import DDGSWebSearchProvider
from mertina_agent.tools.registry import ToolRegistry, registry, tool_error

logger = logging.getLogger(__name__)

WEB_SEARCH_SCHEMA: FunctionDefinition = {
    "name": "web_search",
    "description": (
        "Search the web for information. Returns up to 5 results by default with titles, URLs, "
        "and descriptions. The query is passed through to the configured backend, so operators "
        'such as site:domain, filetype:pdf, intitle:word, -term, and "exact phrase" may work '
        "when the backend supports them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The search query to look up on the web. You may include backend-supported "
                    "operators such as site:example.com, filetype:pdf, intitle:word, -term, or "
                    '"exact phrase".'
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5,
            },
        },
        "required": ["query"],
    },
}


def _clamp_limit(limit: object) -> int:
    """Clamp the requested result count to 1-100, defaulting to 5 when it is not a number."""
    if isinstance(limit, bool) or not isinstance(limit, int | float | str):
        return 5
    try:
        return min(max(int(limit), 1), 100)
    except (TypeError, ValueError, OverflowError):
        return 5


async def web_search_tool(query: object, limit: object = 5, *, provider: WebSearchProvider) -> str:
    """Search the web through ``provider`` and return the JSON result for the model.

    Returns ``{"success": true, "data": {"web": [{"title", "url", "description",
    "position"}, ...]}}`` (metadata only) or ``{"success": false, "error": ...}``.
    """
    if is_interrupted():
        return tool_error("Interrupted", success=False)
    if not isinstance(query, str) or not query.strip():
        return tool_error("query must be a non-empty string", success=False)
    safe_limit = _clamp_limit(limit)
    logger.info("Web search via %s (limit: %d)", provider.name, safe_limit)
    response = await provider.search(query, safe_limit)
    return json.dumps(response, indent=2, ensure_ascii=False)


def register_web_search(
    provider: WebSearchProvider, *, tool_registry: ToolRegistry = registry
) -> None:
    """Register (or re-register) ``web_search`` in the ``web`` toolset, bound to ``provider``.

    The tool is offered only while ``provider.is_available()`` is true.
    """

    async def _handler(args: JsonObject) -> str:
        return await web_search_tool(args.get("query"), args.get("limit", 5), provider=provider)

    tool_registry.register(
        "web_search",
        "web",
        WEB_SEARCH_SCHEMA,
        _handler,
        check_fn=provider.is_available,
        is_async=True,
    )


def build_search_provider(settings: Settings) -> WebSearchProvider:
    """Create the search provider selected by ``settings``."""
    # Only ddgs exists today; the Literal-typed setting rejects anything else.
    return DDGSWebSearchProvider(timeout_s=settings.web_search_timeout_s)


def configure_web_search(settings: Settings, *, tool_registry: ToolRegistry = registry) -> None:
    """Bind ``web_search`` to the provider and timeout configured in ``settings``."""
    register_web_search(build_search_provider(settings), tool_registry=tool_registry)


register_web_search(DDGSWebSearchProvider())
