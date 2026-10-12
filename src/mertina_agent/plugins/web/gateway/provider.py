"""OpenAI Responses web search through the configured model gateway."""

import asyncio
import json
import logging
import re
from urllib import error, request

from mertina_agent.agent.web_search_provider import SearchResponse, WebHit, WebSearchProvider
from mertina_agent.plugins.web._common import search_fail, search_ok, title_hit

logger = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://[^\s<>()\[\]]+")


class GatewayWebSearchProvider(WebSearchProvider):
    """Search provider backed by the gateway's Responses API ``web_search`` tool."""

    def __init__(self, base_url: str, api_key: str, model: str, *, timeout_s: float = 30.0) -> None:
        """Bind the model endpoint and request timeout used for searches."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        """Return the configuration name of this provider."""
        return "gateway"

    def is_available(self) -> bool:
        """Return whether the endpoint and model are configured."""
        return bool(self._base_url and self._model)

    def _search_sync(self, query: str, limit: int) -> list[WebHit]:
        payload = json.dumps(
            {
                "model": self._model,
                "tools": [{"type": "web_search"}],
                "input": f"Search the web for: {query}. Return up to {limit} relevant results.",
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        search_request = request.Request(
            f"{self._base_url}/responses",
            data=payload,
            headers=headers,
        )
        with request.urlopen(search_request, timeout=self._timeout_s) as response:
            data = json.loads(response.read().decode("utf-8"))

        results: list[WebHit] = []
        seen: set[str] = set()
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                description = str(content.get("text", ""))
                for citation in content.get("annotations", []):
                    url = citation.get("url")
                    if citation.get("type") != "url_citation" or not url or url in seen:
                        continue
                    seen.add(url)
                    results.append(
                        title_hit(
                            str(citation.get("title") or url),
                            str(url),
                            description,
                            len(results) + 1,
                        )
                    )
                    if len(results) >= limit:
                        return results
                # Some OpenAI-compatible gateways execute web search but return
                # URLs only in the final text, with an empty annotations array.
                for raw_url in _URL_RE.findall(description):
                    url = raw_url.rstrip(".,;:!?\"'")
                    if url in seen:
                        continue
                    seen.add(url)
                    results.append(title_hit(url, url, description, len(results) + 1))
                    if len(results) >= limit:
                        return results
        return results

    async def search(self, query: str, limit: int = 5) -> SearchResponse:
        """Run one bounded search without blocking the agent event loop."""
        if not self.is_available():
            return search_fail("Gateway web search is not configured")
        try:
            return search_ok(await asyncio.to_thread(self._search_sync, query, limit))
        except (OSError, ValueError, error.URLError) as exc:
            logger.warning("Gateway web search failed: %s", exc)
            return search_fail(f"Gateway web search failed: {exc}")
