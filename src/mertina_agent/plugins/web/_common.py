"""Result-shape helpers shared by the bundled web search providers.

Copied from Hermes plugins/web/_common.py at 4cefeed7debc7091ed65240cbc7e2c36435c0b6b.
Copyright (c) 2025 Nous Research. MIT; see LICENSES/Hermes-Agent-MIT.txt.
Only the search result shapes are kept; extract, keyless and HTTP helpers are
left out with the providers that use them.
"""

from mertina_agent.agent.web_search_provider import SearchFailure, SearchSuccess, WebHit


def search_ok(web_results: list[WebHit]) -> SearchSuccess:
    """Build a successful search response."""
    return {"success": True, "data": {"web": web_results}}


def search_fail(error: str) -> SearchFailure:
    """Build a failed search response."""
    return {"success": False, "error": error}


def title_hit(title: str, url: str, description: str, position: int) -> WebHit:
    """Build a title-first result row, the historical wire shape of the ddgs provider."""
    return {"title": title, "url": url, "description": description, "position": position}
