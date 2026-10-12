"""OpenAI Responses web search through the configured model gateway."""

from __future__ import annotations

import json
from typing import Any, Dict
from urllib import request

from agent.web_search_provider import WebSearchProvider


class GatewayWebSearchProvider(WebSearchProvider):
    """Search provider backed by the Responses API ``web_search`` tool."""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    @property
    def name(self) -> str:
        return "gateway"

    def is_available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        payload = json.dumps({
            "model": self.model,
            "tools": [{"type": "web_search"}],
            "input": f"Search the web for: {query}. Return up to {limit} relevant results.",
        }).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/responses", data=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        rows = []
        seen = set()
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                description = content.get("text", "")
                for citation in content.get("annotations", []):
                    url = citation.get("url")
                    if citation.get("type") != "url_citation" or not url or url in seen:
                        continue
                    seen.add(url)
                    rows.append({
                        "title": citation.get("title") or url,
                        "url": url,
                        "description": description,
                        "position": len(rows) + 1,
                    })
                    if len(rows) >= limit:
                        return {"success": True, "data": {"web": rows}}
        return {"success": True, "data": {"web": rows}}
