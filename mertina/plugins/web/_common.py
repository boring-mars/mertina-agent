# Ported from hermes-agent plugins/web/_common.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Shared plumbing for the bundled web-search provider plugins: the result shapes and the
provider base class."""

from __future__ import annotations

from typing import Any

from mertina.agent.web_search_provider import WebSearchProvider


# --- Result shapes (key order is part of the contract — it reaches the model as JSON) ---
def search_ok(web_results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"success": True, "data": {"web": web_results}}


def search_fail(error: str) -> dict[str, Any]:
    return {"success": False, "error": error}


def title_hit(title: str, url: str, description: str, position: int) -> dict[str, Any]:
    """Title-first row — the historical wire shape of brave/searxng/ddgs/tavily/xai."""
    return {"title": title, "url": url, "description": description, "position": position}


# --- Provider base ---
class BaseWebSearchProvider(WebSearchProvider):
    """Subclasses set ``NAME`` / ``DISPLAY_NAME`` and implement ``is_available``."""

    NAME: str = ""
    DISPLAY_NAME: str = ""

    name = property(lambda self: self.NAME)
    display_name = property(lambda self: self.DISPLAY_NAME)

    def supports_search(self) -> bool:
        return True
