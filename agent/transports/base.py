"""[改动][溯源] ROADMAP.md:70-71：定义模型调用与循环之间的最小响应契约。"""

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class ModelResponse:
    """将流式文本、工具调用及可获得的用量交给循环。"""

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None


class ModelTransport(Protocol):
    """模型协议适配器；中断时抛出 InterruptedError。"""

    def request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Callable[[str], None] | None,
        is_interrupted: Callable[[], bool],
    ) -> ModelResponse:
        """流式获取一轮响应，并在收到文本时立即通知调用方。"""
        ...
