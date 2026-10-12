"""Registry for the single web-search provider used by the v0.1 agent."""

from __future__ import annotations

from typing import Optional

from agent.web_search_provider import WebSearchProvider

_provider: Optional[WebSearchProvider] = None


def register_provider(provider: WebSearchProvider, **_kwargs) -> None:
    """Install the provider used by ``web_search``."""
    global _provider
    if not isinstance(provider, WebSearchProvider):
        raise TypeError("register_provider() expects a WebSearchProvider instance")
    _provider = provider


def get_provider(name: str, **_kwargs) -> Optional[WebSearchProvider]:
    if _provider is not None and _provider.name == name:
        return _provider
    return None


def get_active_search_provider() -> Optional[WebSearchProvider]:
    return _provider


def list_providers(**_kwargs) -> list[WebSearchProvider]:
    return [_provider] if _provider is not None else []


def _reset_for_tests() -> None:
    global _provider
    _provider = None
