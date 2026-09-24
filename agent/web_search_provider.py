# 版本 V0.1 变更说明
# [改动][溯源] Hermes 0469740ab33fd02a4f55a6ea11d81df04ea646a5:agent/web_search_provider.py:1-80；
# ROADMAP.md:82。保留搜索 provider 接口与 Hermes 的 success/data.web/failure 结果协议。
# 已注释的功能：ProviderBase、Hermes 配置层、无密钥和网页提取能力；原版见归档段。
# 源代码改动点：以标准库 ABC 代替缺失的 ProviderBase，以环境变量读取替代 Hermes 配置层。
# 新增代码：在本仓库旧 Brave 实现基础上输出 Hermes 搜索结果格式；旧实现亦以注释保留。

# === Hermes 原版逐行归档：暂不启用的代码以注释保留 ===
# [溯源] 0469740ab33fd02a4f55a6ea11d81df04ea646a5:agent/web_search_provider.py
# """Web Search Provider ABC.

# The single plugin-facing surface every web provider (brave-free, ddgs, searxng,
# exa, parallel, tavily, keenable, firecrawl) implements; registered via
# ``PluginContext.register_web_search_provider()`` and selected by
# ``web.search_backend`` / ``web.extract_backend`` / ``web.backend``.

# Response shapes (legacy contract, the tool wrapper does not translate)::

#     search:  {"success": True, "data": {"web": [{"title", "url", "description", "position"}, ...]}}
#     extract: {"success": True, "data": [{"url", "title", "content", "raw_content", "metadata"}, ...]}
#     failure: {"success": False, "error": str}
# """

# from __future__ import annotations

# import abc
# import os
# from typing import Any, Dict, List

# from agent.provider_base import ProviderBase


# def get_provider_env(name: str) -> str:
#     """Config-aware env lookup (``os.environ`` first, then ``~/.hermes/.env``) so
#     credentials set through the config layer are visible in gateway sessions /
#     delegate children / subprocess runs. Stripped value, or ``""`` when unset.

#     Falls back to a bare ``os.getenv`` when the config module is unavailable (stripped installs, early
#     import contexts). See #40190.
#     """
#     try:
#         from hermes_cli.config import get_env_value

#         val = get_env_value(name)
#     except Exception:  # noqa: BLE001 — config layer optional here
#         val = None
#     if val is None:
#         val = os.getenv(name, "")
#     return (val or "").strip()


# class WebSearchProvider(ProviderBase):
#     """Abstract base class for a web search/extract backend: implement :meth:`is_available`
#     and at least one of :meth:`search` / :meth:`extract`; the ``supports_*`` flags route each capability."""

#     @abc.abstractmethod
#     def is_available(self) -> bool:
#         """True when this provider can service calls. Cheap check only (env var, importable
#         dep, instance URL) — NO network; runs at tool registration and on every ``hermes tools`` paint."""

#     def supports_search(self) -> bool:
#         """True if this provider implements :meth:`search`."""
#         return True

#     def is_keyless_available(self) -> bool:
#         """True when this provider can serve calls WITHOUT credentials (public anonymous
#         free tiers such as Exa / Parallel MCP); used only when NO provider is configured or
#         keyed. Must never make :meth:`is_available` True, or the legacy preference walk would
#         route keyed users onto a higher-priority backend's free tier. Cheap, no network."""
#         return False

#     def supports_extract(self) -> bool:
#         """True if this provider implements :meth:`extract` (sync or ``async def`` —
#         the dispatcher awaits coroutine functions)."""
#         return False

#     def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
#         """Execute a web search. Callers gate on :meth:`supports_search`."""
#         raise NotImplementedError(
#             f"{self.name} does not support search (override supports_search)"
#         )

#     def extract(self, urls: List[str], **kwargs: Any) -> Any:
#         """Extract content from URLs (callers gate on :meth:`supports_extract`); may be ``async def``.
#         Returns ``[{"url", "title", "content", "raw_content", "metadata"?, "error"?}, ...]`` (``error``
#         only on per-URL failure). Ignore unknown ``kwargs`` (``format``, ``include_raw``, ``max_chars``)."""
#         raise NotImplementedError(
#             f"{self.name} does not support extract (override supports_extract)"
#         )


# # ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# # Names external plugins imported from this module before the Sep 2026 decomposition.
# # Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# # The whole block is removed by reverting the commit that added it.
# from typing import Optional  # noqa: F401,E402
# # ---- END PLUGIN-COMPAT ----

# === Mertina v0.1 当前有效实现 ===

"""搜索供应商接口与 v0.1 唯一的 Brave Web Search 实现。"""

import abc
import json
import os
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def get_provider_env(name: str) -> str:
    """从进程环境读取供应商凭据；当前配置层只承诺环境变量来源。"""
    # [改动][溯源] Hermes agent/web_search_provider.py:24-40 先读取 hermes_cli.config；
    # Mertina v0.1 的配置由 run_agent.py 加载环境变量，故仅保留原版回退路径。
    return (os.getenv(name) or "").strip()


class WebSearchProvider(abc.ABC):
    """定义搜索可用性及 Hermes 搜索结果协议，供工具分发层调用。"""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """仅做本地凭据检查，不发起网络请求。"""

    @abc.abstractmethod
    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """返回 success/data.web 结构，单条结果含标题、链接、摘要和序号。"""


class BraveSearchProvider(WebSearchProvider):
    """调用 Brave Web Search，凭据只放在请求头中。"""

    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str | None = None) -> None:
        """保存可选的显式凭据；未提供时在调用时读取环境变量。"""
        self.api_key = api_key

    def is_available(self) -> bool:
        """判断显式凭据或进程环境中的 Brave 凭据是否存在。"""
        return bool(self.api_key or get_provider_env("BRAVE_SEARCH_API_KEY"))

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """请求 Brave，裁剪结果后按 Hermes 的 web 搜索格式返回。"""
        # [改动][溯源] 旧 Mertina BraveSearchProvider.search 返回列表；
        # Hermes agent/web_search_provider.py:8-12 与 tools/web_tools.py:268-273 约定统一结果封套。
        token = self.api_key or get_provider_env("BRAVE_SEARCH_API_KEY")
        if not token:
            raise RuntimeError("BRAVE_SEARCH_API_KEY is required for web_search")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search query must be non-empty")
        # [改动][溯源] Brave Web Search API count 范围 1-20：
        # https://api-dashboard.search.brave.com/api-reference/web/search/get
        limit = min(max(int(limit), 1), 20)
        url = f"{self.endpoint}?{urlencode({'q': query, 'count': limit})}"
        request = Request(url, headers={"Accept": "application/json", "X-Subscription-Token": token})
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
        results = payload.get("web", {}).get("results", [])
        web = [
            {"title": str(item.get("title") or ""),
             "url": str(item.get("url") or ""),
             "description": str(item.get("description") or "")[:1000],
             "position": index}
            for index, item in enumerate((item for item in results[:limit] if isinstance(item, dict)), 1)
        ]
        return {"success": True, "data": {"web": web}}


# === Mertina 改动前实现：返回列表的旧协议按原样注释保留 ===
# """[改动][溯源] ROADMAP.md:82、Brave Web Search API：隔离联网搜索实现。"""
#
# import json
# import os
# from typing import Protocol
# from urllib.parse import urlencode
# from urllib.request import Request, urlopen
#
#
# class SearchProvider(Protocol):
#     """搜索工具依赖的可替换接口。"""
#
#     def search(self, query: str, count: int = 5) -> list[dict[str, str]]:
#         """返回标题、链接和摘要组成的结果列表。"""
#         ...
#
#
# class BraveSearchProvider:
#     """调用 Brave Web Search，密钥只放在请求头中。"""
#
#     endpoint = "https://api.search.brave.com/res/v1/web/search"
#
#     def __init__(self, api_key: str | None = None) -> None:
#         """接受显式密钥或在首次搜索时读取环境变量。"""
#         self.api_key = api_key
#
#     def search(self, query: str, count: int = 5) -> list[dict[str, str]]:
#         """请求并裁剪搜索结果，避免把上游完整响应塞入模型历史。"""
#         token = self.api_key or os.getenv("BRAVE_SEARCH_API_KEY")
#         if not token:
#             raise RuntimeError("BRAVE_SEARCH_API_KEY is required for web_search")
#         if not isinstance(query, str) or not query.strip():
#             raise ValueError("search query must be non-empty")
#         count = max(1, min(int(count), 10))
#         url = f"{self.endpoint}?{urlencode({'q': query, 'count': count})}"
#         request = Request(url, headers={"Accept": "application/json", "X-Subscription-Token": token})
#         with urlopen(request, timeout=15) as response:
#             payload = json.load(response)
#         results = payload.get("web", {}).get("results", [])
#         return [
#             {"title": str(item.get("title") or ""),
#              "url": str(item.get("url") or ""),
#              "description": str(item.get("description") or "")[:1000]}
#             for item in results[:count]
#             if isinstance(item, dict)
#         ]
