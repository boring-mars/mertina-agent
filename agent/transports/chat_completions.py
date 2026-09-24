"""[改动][溯源] ROADMAP.md:70-72：将 Chat Completions 流转换为循环可用的响应。"""

from typing import Any, Callable
from uuid import uuid4

from agent.transports.base import ModelResponse


def _field(value: Any, name: str, default: Any = None) -> Any:
    """兼容 OpenAI SDK 对象和测试客户端返回的字典。"""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class ChatCompletionsTransport:
    """向 OpenAI 兼容端点发起流式请求，并组装文本及函数调用片段。"""

    def __init__(self, client: Any, model: str) -> None:
        """保存可注入的客户端及模型名。"""
        self.client = client
        self.model = model

    def request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text_delta: Callable[[str], None] | None,
        is_interrupted: Callable[[], bool],
    ) -> ModelResponse:
        """逐块转发文本；按 index 合并工具参数，保持调用 ID 与结果配对。"""
        if is_interrupted():
            raise InterruptedError("Agent turn interrupted")
        request: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            request["tools"] = tools
        chunks = self.client.chat.completions.create(**request)
        content_parts: list[str] = []
        pending: dict[int, dict[str, str]] = {}
        usage: dict[str, int] = {}
        finish_reason = None
        try:
            for chunk in chunks:
                if is_interrupted():
                    raise InterruptedError("Agent turn interrupted")
                chunk_usage = _field(chunk, "usage")
                if chunk_usage:
                    usage = {
                        key: int(_field(chunk_usage, key, 0) or 0)
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    }
                choices = _field(chunk, "choices", []) or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = _field(choice, "finish_reason") or finish_reason
                delta = _field(choice, "delta")
                text = _field(delta, "content") or ""
                if text:
                    content_parts.append(text)
                    if on_text_delta:
                        on_text_delta(text)
                for call in _field(delta, "tool_calls", []) or []:
                    index = int(_field(call, "index", 0) or 0)
                    slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    slot["id"] = slot["id"] or (_field(call, "id") or "")
                    function = _field(call, "function")
                    slot["name"] += _field(function, "name", "") or ""
                    slot["arguments"] += _field(function, "arguments", "") or ""
            if is_interrupted():
                raise InterruptedError("Agent turn interrupted")
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                close()
        calls = [
            {
                "id": slot["id"] or f"call_{uuid4().hex}",
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
            }
            for _, slot in sorted(pending.items())
        ]
        return ModelResponse("".join(content_parts), calls, usage, finish_reason)
