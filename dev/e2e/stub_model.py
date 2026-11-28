"""Deterministic OpenAI-compatible Chat Completions endpoint for end-to-end tests.

The reply depends only on the request messages, so every database backend
stores exactly the same conversation. Markers in the latest user message pick
the behaviour:

- ``[tool]``: ask for the ``lookup`` tool, then answer from its result.
- ``[parallel]``: ask for two ``lookup`` calls in one message.
- ``[toolerror]``: ask for ``lookup`` with ``fail: true``; ``serve_gateway.py``
  answers that call with an error result.
- ``[slow]``: stream many small chunks slowly, so a run can be stopped mid-reply.
- ``[tokens]``: stream hundreds of one-character chunks slowly, more than the
  Gateway's default 256-event buffer, so a late reconnect gets 409.
- ``[long]``: answer with more than 64 KiB of mixed-script Unicode text.
- ``[retry]``: the first request answers 429 with ``Retry-After``; the same
  request then gets the default answer.
- ``[fail]``: always answer 503, which the agent retries before failing.
- otherwise: a short answer that names the turn number.

Only ``[retry]`` is stateful: the server counts attempts per distinct request
body (its messages), in memory, for the life of the process. Every other reply
depends only on the request messages.

Run with ``python dev/e2e/stub_model.py --port 18090``.
"""

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from itertools import cycle, islice

from aiohttp import web

SLOW_CHUNKS = 40
SLOW_DELAY_S = 0.05
TOKEN_CHUNKS = 600
TOKEN_DELAY_S = 0.01
TOKEN_TEXT = "Mertina streams one character at a time. 一字一字地输出。"
RETRY_AFTER_S = 1
ATTEMPTS_KEY = web.AppKey("attempts", dict[str, int])
LONG_TEXT_BYTES = 100 * 1024
LONG_UNIT = "汉字🙂 Mertina ünïcödé · 東京 · Ελληνικά · "
DEFAULT_PADDING = " detail" * 60


@dataclass(frozen=True)
class Reply:
    """What the stub answers: text chunks, tool calls, or an HTTP error status."""

    chunks: tuple[str, ...] = ()
    delay_s: float = 0.0
    tool_calls: tuple[dict[str, str], ...] = ()
    error_status: int | None = None
    retry_once: bool = False
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    )

    @property
    def text(self) -> str:
        """The complete answer text."""
        return "".join(self.chunks)

    @property
    def tool_call(self) -> dict[str, str] | None:
        """The first tool call, if any."""
        return self.tool_calls[0] if self.tool_calls else None


def _split(text: str, size: int) -> tuple[str, ...]:
    return tuple(text[index : index + size] for index in range(0, len(text), size)) or ("",)


def long_text() -> str:
    """Mixed-script text just over LONG_TEXT_BYTES when encoded as UTF-8."""
    unit_bytes = len(LONG_UNIT.encode("utf-8"))
    return LONG_UNIT * (LONG_TEXT_BYTES // unit_bytes + 1)


def _lookup(identifier: str, **arguments: object) -> dict[str, str]:
    return {
        "id": identifier,
        "name": "lookup",
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }


def plan_reply(messages: list[dict[str, object]]) -> Reply:
    """Decide the answer from the request messages alone."""
    last = messages[-1]
    turn = sum(1 for message in messages if message.get("role") == "user")
    if last.get("role") == "tool":
        return Reply(_split(f"Lookup finished: {last.get('content')}", 64))
    text = str(last.get("content") or "")
    query = " ".join(text.split())
    for marker in ("[tool]", "[parallel]", "[toolerror]"):
        query = query.replace(marker, "").strip()
    if "[fail]" in text:
        return Reply(error_status=503)
    if "[parallel]" in text:
        return Reply(
            tool_calls=(
                _lookup(f"call_{turn}_a", query=f"{query} (first)"),
                _lookup(f"call_{turn}_b", query=f"{query} (second)"),
            )
        )
    if "[toolerror]" in text:
        return Reply(tool_calls=(_lookup(f"call_{turn}", query=query, fail=True),))
    if "[tool]" in text:
        return Reply(tool_calls=(_lookup(f"call_{turn}", query=query),))
    if "[slow]" in text:
        return Reply(tuple(f"word{index} " for index in range(SLOW_CHUNKS)), SLOW_DELAY_S)
    if "[tokens]" in text:
        return Reply(tuple(islice(cycle(TOKEN_TEXT), TOKEN_CHUNKS)), TOKEN_DELAY_S)
    if "[long]" in text:
        return Reply(_split(long_text(), 2000))
    summary = " ".join(text.split())[:80]
    reply = Reply(_split(f"Reply {turn}: {summary}.{DEFAULT_PADDING}", 120))
    if "[retry]" in text:
        return Reply(reply.chunks, retry_once=True)
    return reply


def _error(status: int, *, retry_after_s: int | None = None) -> web.Response:
    headers = {} if retry_after_s is None else {"Retry-After": str(retry_after_s)}
    body = {"error": {"message": f"Stub model error {status}", "type": "stub_error"}}
    return web.json_response(body, status=status, headers=headers)


def _first_attempt(request: web.Request, messages: list[dict[str, object]]) -> bool:
    """Count this request body's attempts; true only for the first one."""
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
    key = hashlib.sha256(encoded).hexdigest()
    attempts = request.app[ATTEMPTS_KEY]
    attempts[key] = attempts.get(key, 0) + 1
    return attempts[key] == 1


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
        if reply.tool_calls:
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": index,
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }
                    for index, call in enumerate(reply.tool_calls)
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
    if reply.error_status is not None:
        return _error(reply.error_status)
    if reply.retry_once and _first_attempt(request, body["messages"]):
        return _error(429, retry_after_s=RETRY_AFTER_S)
    if body.get("stream"):
        return await _stream(request, body, reply)
    message: dict[str, object] = {"role": "assistant", "content": reply.text or None}
    finish = "stop"
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in reply.tool_calls
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
    app[ATTEMPTS_KEY] = {}
    app.add_routes([web.post("/v1/chat/completions", completions)])
    web.run_app(app, host="127.0.0.1", port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
