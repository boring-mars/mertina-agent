"""[改动][溯源] ROADMAP.md:84-85：从 TOML 与环境变量读取 v0.1 配置。"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentSettings:
    """保存模型连接及唯一搜索 provider 的启动配置。"""

    base_url: str | None
    model: str | None
    api_key: str | None
    max_iterations: int
    max_retries: int
    search_provider: str


def load_settings(path: str | None = None) -> AgentSettings:
    """读取可选 TOML 文件，再用环境变量覆盖其模型与搜索设置。"""
    config = {}
    if path is not None:
        with Path(path).open("rb") as file:
            config = tomllib.load(file)
    model = config.get("model", {})
    search = config.get("search", {})
    if not isinstance(model, dict) or not isinstance(search, dict):
        raise ValueError("model and search config sections must be tables")
    provider = os.getenv("MERTINA_SEARCH_PROVIDER", search.get("provider", "brave"))
    # [改动][溯源] ROADMAP.md:82；无搜索凭据时也允许显式关闭工具。
    # if provider != "brave":
    if provider not in {"brave", "none"}:
        raise ValueError(f"unsupported search provider: {provider}")
    return AgentSettings(
        base_url=os.getenv("MERTINA_BASE_URL", model.get("base_url")),
        model=os.getenv("MERTINA_MODEL", model.get("name")),
        api_key=os.getenv("OPENAI_API_KEY", model.get("api_key")),
        max_iterations=int(os.getenv("MERTINA_MAX_ITERATIONS", model.get("max_iterations", 10))),
        max_retries=int(os.getenv("MERTINA_MAX_RETRIES", model.get("max_retries", 2))),
        search_provider=provider,
    )
