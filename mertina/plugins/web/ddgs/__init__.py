# Ported from hermes-agent plugins/web/ddgs/__init__.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""DuckDuckGo search plugin (``ddgs`` package, optional dep) — bundled, auto-loaded."""

from __future__ import annotations

from mertina.plugins.web.ddgs.provider import DDGSWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(DDGSWebSearchProvider())
