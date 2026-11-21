"""Deterministic OpenAI-compatible Chat Completions endpoint for end-to-end tests.

The reply depends only on the request messages, so every database backend
stores exactly the same conversation. Markers in the latest user message pick
the behaviour:

- ``[tool]``: ask for the ``lookup`` tool, then answer from its result.
- ``[slow]``: stream many small chunks slowly, so a run can be stopped mid-reply.
- ``[long]``: answer with more than 64 KiB of mixed-script Unicode text.
- otherwise: a short answer that names the turn number.

Run with ``python dev/e2e/stub_model.py --port 18090``.
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field

from aiohttp import web

SLOW_CHUNKS = 40
SLOW_DELAY_S = 0.05
LONG_TEXT_BYTES = 100 * 1024
LONG_UNIT = "汉字🙂 Mertina ünïcödé · 東京 · Ελληνικά · "
DEFAULT_PADDING = " detail" * 60


@dataclass(frozen=True)
class Reply:
    """What the stub answers: text chunks, or one tool call."""

    chunks: tuple[str, ...] = ()
    delay_s: float = 0.0
    tool_call: dict[str, str] | None = None
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    )

    @property
    def text(self) -> str:
        """The complete answer text."""
        return "".join(self.chunks)


def _split(text: str, size: int) -> tuple[str, ...]:
    return tuple(text[index : index + size] for index in range(0, len(text), size)) or ("",)


def long_text() -> str:
    """Mixed-script text just over LONG_TEXT_BYTES when encoded as UTF-8."""
    unit_bytes = len(LONG_UNIT.encode("utf-8"))
    return LONG_UNIT * (LONG_TEXT_BYTES // unit_bytes + 1)


def plan_reply(messages: list[dict[str, object]]) -> Reply:
    """Decide the answer from the request messages alone."""
    last = messages[-1]
    turn = sum(1 for message in messages if message.get("role") == "user")
    if last.get("role") == "tool":
        return Reply(_split(f"Lookup finished: {last.get('content')}", 64))
    text = str(last.get("content") or "")
    if "[tool]" in text:
        query = text.replace("[tool]", "").strip()
        arguments = json.dumps({"query": query}, ensure_ascii=False)
        return Reply(tool_call={"id": f"call_{turn}", "name": "lookup", "arguments": arguments})
    if "[slow]" in text:
        return Reply(tuple(f"word{index} " for index in range(SLOW_CHUNKS)), SLOW_DELAY_S)
    if "[long]" in text:
        return Reply(_split(long_text(), 2000))
    summary = " ".join(text.split())[:80]
    return Reply(_split(f"Reply {turn}: {summary}.{DEFAULT_PADDING}", 120))


def _chunk(model: str, delta: dict[str, object], finish: str | None) -> bytes:
    payload = {
        "id": "chatcmpl-e2e",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


async def _stream(
    request: web.Request, body: dict[str, object], reply: Reply
) -> web.StreamResponse:
    model = str(body["model"])
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    try:
        if reply.tool_call is not None:
            call = reply.tool_call
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }
                ],
            }
            await response.write(_chunk(model, delta, None))
            await response.write(_chunk(model, {}, "tool_calls"))
        else:
            for index, piece in enumerate(reply.chunks):
                if reply.delay_s:
                    await asyncio.sleep(reply.delay_s)
                delta: dict[str, object] = {"content": piece}
                if index == 0:
                    delta["role"] = "assistant"
                await response.write(_chunk(model, delta, None))
            await response.write(_chunk(model, {}, "stop"))
        options = body.get("stream_options")
        if isinstance(options, dict) and options.get("include_usage"):
            usage = {
                "id": "chatcmpl-e2e",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": reply.usage,
            }
            await response.write(f"data: {json.dumps(usage)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        # The agent stopped reading, for example after a stop request.
        return response
    return response


async def completions(request: web.Request) -> web.StreamResponse:
    """Serve ``POST /v1/chat/completions``, streaming or not."""
    body = await request.json()
    reply = plan_reply(body["messages"])
    if body.get("stream"):
        return await _stream(request, body, reply)
    message: dict[str, object] = {"role": "assistant", "content": reply.text or None}
    finish = "stop"
    if reply.tool_call is not None:
        call = reply.tool_call
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
        ]
        finish = "tool_calls"
    return web.json_response(
        {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": reply.usage,
        }
    )


def main() -> None:
    """Listen on loopback until terminated."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.add_routes([web.post("/v1/chat/completions", completions)])
    web.run_app(app, host="127.0.0.1", port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
