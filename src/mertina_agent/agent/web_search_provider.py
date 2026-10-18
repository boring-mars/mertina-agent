"""The interface every web search backend implements, and its result shapes.

Copied from Hermes agent/web_search_provider.py (``WebSearchProvider``) and
agent/provider_base.py (``ProviderBase.name``) at
4cefeed7debc7091ed65240cbc7e2c36435c0b6b. Copyright (c) 2025 Nous Research.
MIT; see LICENSES/Hermes-Agent-MIT.txt and docs/sources/hermes-agent-core.md.

The response shape is Hermes's legacy contract, which reaches the model as JSON
unchanged::

    success: {"success": True, "data": {"web": [{"title", "url", "description", "position"}]}}
    failure: {"success": False, "error": str}

Extract support, keyless tiers and the setup picker are left out. ``search`` is
asynchronous because Mertina's loop is.
"""

import abc
from typing import Literal, TypedDict


class WebHit(TypedDict):
    """One search result; key order is part of the contract that reaches the model."""

    title: str
    url: str
    description: str
    position: int


class WebResults(TypedDict):
    """The ``data`` payload of a successful search."""

    web: list[WebHit]


class SearchSuccess(TypedDict):
    """A successful search response."""

    success: Literal[True]
    data: WebResults


class SearchFailure(TypedDict):
    """A failed search response; ``error`` is readable by the model."""

    success: Literal[False]
    error: str


type SearchResponse = SearchSuccess | SearchFailure


class WebSearchProvider(abc.ABC):
    """Abstract base class for a web search backend."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used as the provider's configuration value."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return whether this provider can serve calls.

        A cheap check only (importable dependency, configured value); no network
        I/O, because it runs whenever the tool list is built.
        """

    @abc.abstractmethod
    async def search(self, query: str, limit: int = 5) -> SearchResponse:
        """Run a web search; failures are returned as :class:`SearchFailure`, never raised."""
