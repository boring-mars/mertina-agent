# Ported from hermes-agent agent/chat_completion_helpers.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""API-call helpers extracted from :class:`AIAgent`: the non-streaming request driver, request
kwargs builder, assistant-message materializer and max-iterations handler.

Each function takes the parent ``AIAgent`` as ``agent``; AIAgent keeps thin forwarders.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

from mertina.agent.chat_completion_stream_monitor import StreamingWaitMonitor
from mertina.agent.errors import EmptyStreamError
from mertina.agent.message_content import flatten_message_text
from mertina.agent.message_metadata import append_message, stamp_message_timestamp
from mertina.agent.message_sanitization import (
    _sanitize_surrogates,
)
from mertina.agent.transports.base import ProviderTransport
from mertina.agent.transports.types import NormalizedResponse
from mertina.constants import FINISH_REASON_LENGTH, PARTIAL_STREAM_STUB_ID
from mertina.utils import env_float, env_int

logger = logging.getLogger(__name__)


def _context_thread_target(callback: Callable[[], Any]) -> Callable[[], Any]:
    """Bind a no-argument thread target to the caller's ContextVars."""
    context = contextvars.copy_context()
    return lambda: context.run(callback)


def _iter_provider_stream_chunks(stream: Any) -> Iterator[Any]:
    """Yield SDK chunks."""
    yield from stream


def _dispatch_nonstreaming_api_request(
    agent: Any, api_kwargs: dict[str, Any], *, make_client: Callable[[str], Any]
) -> Any:
    """Run one non-streaming LLM request and return it.

    ``make_client(reason)`` builds the per-request client so callers can register it with their
    abort/close machinery. Interrupt/abort/close semantics stay in callers.
    """
    request_client = make_client("chat_completion_request")
    return request_client.chat.completions.create(**api_kwargs)


class _RequestClientRegistry:
    """Per-request client registry shared by the request worker and the stranger
    thread (interrupt loop) that may abort it.

    Thread-ownership rule (#29507): the owning worker pops + fully closes on its
    way out. A *stranger* thread only aborts the sockets — never ``client.close()``
    — avoiding the FD-recycling race where a just-closed TLS FD was reassigned to
    ``kanban.db`` and the live SSL BIO wrote into the SQLite header. The abort
    happens under the lock: once released the worker may cache the client and the
    NEXT call check it out.
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.client: Any = None
        self.owner_tid: int | None = None
        self.lock = threading.Lock()

    def set_client(self, client: Any) -> Any:
        with self.lock:
            self.client, self.owner_tid = client, threading.get_ident()
        return client

    def close_once(self, reason: str) -> None:
        with self.lock:
            request_client, owner_tid = self.client, self.owner_tid
            stranger_thread = (
                request_client is not None
                and owner_tid is not None
                and owner_tid != threading.get_ident()
            )
            if stranger_thread:
                abort = self.agent._abort_request_openai_client
                abort(request_client, reason=reason)
                return
            self.client = None
            self.owner_tid = None
        if request_client is None:
            return
        self.agent._close_request_openai_client(request_client, reason=reason)


def interruptible_api_call(agent: Any, api_kwargs: dict[str, Any]) -> Any:
    """Run the API call on a worker thread so the caller can detect interrupts
    without waiting for the full HTTP round-trip. Each worker gets its own
    per-request client (interrupts close only that one)."""
    from mertina.agent.chat_completion_nonstream import _NonStreamRequest

    return _NonStreamRequest(agent, api_kwargs).run()


def _build_chat_completions_kwargs(
    agent: Any, api_messages: list[dict[str, Any]], tools_for_api: list[dict[str, Any]] | None
) -> dict[str, Any]:
    transport: ProviderTransport = agent._get_transport()

    _common: dict[str, Any] = dict(  # noqa: C408  # upstream's dict() call
        model=agent.model,
        messages=api_messages,
        tools=tools_for_api,
        max_tokens=agent.max_tokens,
        max_tokens_param_fn=agent._max_tokens_param,
    )

    return transport.build_kwargs(
        **_common,
    )


def build_api_kwargs(
    agent: Any,
    api_messages: list[dict[str, Any]],
    tools_for_api: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the keyword arguments dict for the chat-completions API."""
    kwargs = _build_api_kwargs_for_mode(agent, api_messages, tools_for_api)
    return kwargs


def _build_api_kwargs_for_mode(
    agent: Any,
    api_messages: list[dict[str, Any]],
    tools_for_api: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if tools_for_api is None:
        tools_for_api = agent.tools
    builder = _build_chat_completions_kwargs
    return builder(agent, api_messages, tools_for_api)


def _model_dump_safe(obj: Any) -> Any:
    """``model_dump(warnings=False)`` (avoids pydantic serializer UserWarnings on
    generic-union SDK models), falling back for shims that reject the kwarg."""
    try:
        return obj.model_dump(warnings=False)
    except TypeError:
        return obj.model_dump()


def _dump_if_model(value: Any) -> Any:
    return _model_dump_safe(value) if hasattr(value, "model_dump") else value


def _assistant_reasoning_text(agent: Any, assistant_message: Any) -> str | None:
    """Structured reasoning, else inline ``<think>`` blocks embedded in content."""
    reasoning_text: str | None = agent._extract_reasoning(assistant_message)
    if not reasoning_text:
        content = flatten_message_text(getattr(assistant_message, "content", None))
        think_blocks = re.findall(r"<think>(.*?)</think>", content, flags=re.DOTALL)
        if think_blocks:
            reasoning_text = "\n\n".join(b.strip() for b in think_blocks if b.strip()) or None
    if reasoning_text and agent.verbose_logging:
        logging.debug(f"Captured reasoning ({len(reasoning_text)} chars): {reasoning_text}")
    return _sanitize_surrogates(reasoning_text) if reasoning_text else reasoning_text


def _assistant_content_for_storage(agent: Any, assistant_message: Any) -> str:
    # Sanitize surrogates (Kimi/GLM via Ollama emit code points that crash json.dumps), then
    # strip inline <think> tags at the storage boundary (they leaked to platforms and
    # polluted titles).
    content = _sanitize_surrogates(
        flatten_message_text(getattr(assistant_message, "content", None))
    )
    if isinstance(content, str) and content:
        content = agent._strip_think_blocks(content).strip()
    return content


def _assistant_tool_call_dict(agent: Any, tool_call: Any, index: int) -> dict[str, Any]:
    raw_id = getattr(tool_call, "id", None)
    call_id = getattr(tool_call, "call_id", None)
    if not isinstance(call_id, str) or not call_id.strip():
        if isinstance(raw_id, str) and raw_id.strip():
            call_id = raw_id.strip()
    call_id = call_id.strip()  # type: ignore[union-attr]  # chat-completions tool calls carry an id

    # Arguments are deliberately NOT redacted: this dict is replayed to the model every
    # turn, so a ``***`` mask would break credential-dependent commands (#43083).
    tc_dict: dict[str, Any] = {
        "id": call_id,
        "type": tool_call.type,
        "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments},
    }
    # Preserve extra_content (Gemini thought_signature) or Gemini 3 thinking
    # models 400 on the next request.
    extra = getattr(tool_call, "extra_content", None)
    if extra is not None:
        tc_dict["extra_content"] = _dump_if_model(extra)
    return tc_dict


def build_assistant_message(
    agent: Any, assistant_message: Any, finish_reason: str
) -> dict[str, Any]:
    """Build a normalized assistant message dict (reasoning, reasoning_details,
    optional tool_calls) shared by the tool-call and final-response paths.
    Textless turns are NOT padded here."""
    assistant_tool_calls = getattr(assistant_message, "tool_calls", None)
    reasoning_text = _assistant_reasoning_text(agent, assistant_message)
    msg: dict[str, Any] = stamp_message_timestamp(
        {
            "role": "assistant",
            "content": _assistant_content_for_storage(agent, assistant_message),
            "reasoning": reasoning_text,
            "finish_reason": finish_reason,
        }
    )

    raw_reasoning_content = getattr(assistant_message, "reasoning_content", None)
    if raw_reasoning_content is None:
        model_extra = getattr(assistant_message, "model_extra", None) or {}
        if isinstance(model_extra, dict) and "reasoning_content" in model_extra:
            raw_reasoning_content = model_extra["reasoning_content"]
    if raw_reasoning_content is not None:
        msg["reasoning_content"] = _sanitize_surrogates(raw_reasoning_content)
    elif reasoning_text:
        # Streaming-only providers accumulate reasoning via deltas and never set
        # it on the message; replaying through a thinking model then 400s.
        # Promote ONLY when nothing set the field: SDK reasoning_content wins, and
        # reasoning-less turns leave the field absent.
        msg["reasoning_content"] = reasoning_text

    if getattr(assistant_message, "reasoning_details", None):
        # Preserve reasoning_details exactly (opaque signature /
        # encrypted_content fields) for cross-turn reasoning continuity.
        preserved = []
        for d in assistant_message.reasoning_details:
            if isinstance(d, dict):
                preserved.append(d)
            elif hasattr(d, "__dict__"):
                preserved.append(d.__dict__)
            elif hasattr(d, "model_dump"):
                preserved.append(_model_dump_safe(d))
        if preserved:
            msg["reasoning_details"] = preserved

    if assistant_tool_calls:
        msg["tool_calls"] = [
            _assistant_tool_call_dict(agent, tc, i) for i, tc in enumerate(assistant_tool_calls)
        ]
    return msg


# Keys outside the Chat Completions schema that strict gateways (Fireworks-backed OpenCode
# Go, Mistral, Moonshot/Kimi) reject with 422. The transport's convert_messages() drops them
# in the main loop; the summary path calls chat.completions.create() directly, so mirror it.
_SUMMARY_FOREIGN_MESSAGE_KEYS = (
    "reasoning",
    "finish_reason",
    "tool_name",
    "codex_reasoning_items",
    "codex_message_items",
    "timestamp",
    "platform_message_id",
)


_EMPTY_SUMMARY_RESPONSE = "I reached the iteration limit and couldn't generate a summary."


def _iteration_summary_api_messages(
    agent: Any, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Wire-ready messages for the summary call, mirroring the main loop's api_messages build
    (reasoning replay, schema-foreign key strip, underscore-key sweep)."""
    api_messages = []
    for msg in messages:
        api_msg = msg.copy()
        agent._copy_reasoning_content_for_api(msg, api_msg)
        for key in _SUMMARY_FOREIGN_MESSAGE_KEYS:
            api_msg.pop(key, None)
        # Mirror of the transport's role-qualified strip: ``name`` is
        # schema-foreign on tool results only (strict providers reject with
        # "contains item with unknown key name"); it stays on user/assistant.
        if api_msg.get("role") == "tool":
            api_msg.pop("name", None)
        api_messages.append(api_msg)

    effective_system = agent._cached_system_prompt or ""
    if effective_system:
        api_messages = [{"role": "system", "content": effective_system}] + api_messages  # noqa: RUF005  # upstream's expression

    for api_msg in api_messages:  # underscore scaffolding: the transport's sweeper is bypassed here
        if isinstance(api_msg, dict):
            for internal_key in [k for k in api_msg if isinstance(k, str) and k.startswith("_")]:
                del api_msg[internal_key]
    return api_messages


def _summary_text(agent: Any, response: Any, **normalize_kwargs: Any) -> str:
    normalized: NormalizedResponse = agent._get_transport().normalize_response(
        response, **normalize_kwargs
    )
    if normalized.tool_calls:
        # No summary path executes tool calls; log so a tool-only response that falls into the
        # empty-summary retry is diagnosable.
        logger.warning("Iteration summary emitted tool calls; discarding them")
    return (normalized.content or "").strip()


def _chat_summary_attempt(agent: Any, api_messages: list[dict[str, Any]]) -> Callable[[int], str]:
    # Same kwargs builder as the main loop so the summary keeps the cached prefix. Do not omit
    # tools or force tool_choice="none" here: SGLang renders the prompt with tools=None in that
    # mode and the KV prefix diverges.
    summary_kwargs = agent._build_api_kwargs(api_messages)

    def _attempt(retry_count: int) -> str:
        summary_client = agent._ensure_primary_openai_client(
            reason="iteration_limit_summary_retry" if retry_count else "iteration_limit_summary"
        )
        response = summary_client.chat.completions.create(**summary_kwargs)
        return _summary_text(agent, response)

    return _attempt


def handle_max_iterations(agent: Any, messages: list[dict[str, Any]], api_call_count: int) -> str:
    """Request a summary when max iterations are reached. Returns the final response text."""
    warning = f"⚠️  Reached maximum iterations ({agent.max_iterations}). Requesting summary..."
    if getattr(agent, "suppress_status_output", False):
        # Strict machine-readable mode (-Q, oneshot): keep diagnostics off stdout. quiet_mode is
        # NOT the gate — the interactive CLI runs quiet_mode=True by default and must see this.
        logger.warning(warning)
    else:
        agent._safe_print(warning, diagnostic=True)

    # Shared constant so compaction recognizers can identify this runtime nudge by its stable
    # content after SessionDB projection strips metadata flags.
    from mertina.agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST

    append_message(messages, {"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST})

    try:
        api_messages = _iteration_summary_api_messages(agent, messages)
        build_attempt = _chat_summary_attempt
        attempt = build_attempt(agent, api_messages)

        # One retry on an empty summary; a summary empty once its <think> block is stripped is
        # NOT retried.
        final_response = _EMPTY_SUMMARY_RESPONSE
        for retry_count in (0, 1):
            text = attempt(retry_count)
            if not text:
                continue
            if "<think>" in text:
                text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
            if text:
                append_message(messages, {"role": "assistant", "content": text})
                final_response = text
            break

    except Exception as e:
        logger.warning("Failed to get summary response: %s", e)
        from mertina.agent.turn_failure_copy import site_copy

        final_response = site_copy("max_iterations_no_summary", limit=agent.max_iterations)

    return final_response


def _build_partial_stream_stub(
    role: str,
    full_content: str | None,
    full_reasoning: str | None,
    model_name: Any,
    usage_obj: Any,
) -> SimpleNamespace:
    """Stub for an SSE stream that ended without ``finish_reason`` after
    delivering content. Tagged ``PARTIAL_STREAM_STUB_ID`` + ``FINISH_REASON_LENGTH``
    instead of accepting truncated output as a complete turn (#32086).
    """
    return SimpleNamespace(
        id=PARTIAL_STREAM_STUB_ID,
        model=model_name,
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(
                    role=role,
                    content=full_content,
                    tool_calls=None,
                    reasoning_content=full_reasoning,
                ),
                finish_reason=FINISH_REASON_LENGTH,
            )
        ],
        usage=usage_obj,
    )


# SSE error events from proxies (OpenRouter's {"error":{"message":"Network
# connection lost."}}) surface as SDK APIError without a status_code (unlike
# APIStatusError). They mean the upstream stream died: retry with a fresh
# connection like an httpx drop.
_SSE_CONN_PHRASES = (
    "connection lost",
    "connection reset",
    "connection closed",
    "connection terminated",
    "network error",
    "network connection",
    "terminated",
    "peer closed",
    "broken pipe",
    "upstream connect error",
)


def _rejects_stream_options(exc: BaseException) -> bool:
    """A 400/422 whose body names ``stream_options`` as an unknown/extra field: strict
    OpenAI-compatible endpoints (Azure AI Foundry MaaS, Pydantic ``extra_forbidden``) reject
    the usage extension outright (#9705). Distinct from "stream not supported", which flips
    the whole session to non-streaming."""
    if getattr(exc, "status_code", None) not in (400, 422):
        return False
    body = f"{getattr(exc, 'body', '') or ''} {exc}".lower()
    return "stream_options" in body and any(
        k in body for k in ("extra", "not supported", "unrecognized", "unexpected", "unknown")
    )


def _is_sse_connection_error(exc: BaseException) -> bool:
    from openai import APIError as _APIError

    if not isinstance(exc, _APIError) or getattr(exc, "status_code", None):
        return False
    err_lower = str(exc).lower()
    return any(phrase in err_lower for phrase in _SSE_CONN_PHRASES)


def _with_stream_emitters(agent: Any, run: Callable[[], Any]) -> Any:
    """Run ``run()`` and return its response."""
    response = run()
    return response


class _ToolCallAccumulator:
    """Assemble streamed tool-call deltas into complete ``tool_calls`` entries
    (``acc``: slot index -> entry dict). Ollama-compatible endpoints reuse index 0
    for every call in a parallel batch, distinguishing them only by id, so a new
    id at an already-seen raw index is redirected to a fresh slot."""

    def __init__(self) -> None:
        self.acc: dict[int, dict[str, Any]] = {}
        self._notified: set[int] = set()
        self._last_id_at_idx: dict[int, str] = {}  # raw_index -> last seen non-empty id
        self._active_slot_by_idx: dict[int, int] = {}  # raw_index -> current slot in acc
        # Argument deltas are collected per slot and joined once in ``materialize`` —
        # ``+=`` per chunk rebuilds the whole string every delta (quadratic on big args).
        self._argument_parts: dict[int, list[str]] = {}

    def materialize(self) -> dict[int, dict[str, Any]]:
        """Join buffered argument deltas into each entry's ``arguments``; idempotent. Returns
        ``acc``."""
        for idx, parts in self._argument_parts.items():
            self.acc[idx]["function"]["arguments"] = "".join(parts)
        return self.acc

    def feed(self, tc_delta: Any) -> str | None:
        """Merge one delta; return the tool name the first time it is complete."""
        raw_idx = getattr(tc_delta, "index", None)
        if raw_idx is None:
            raw_idx = 0
        tc_id = getattr(tc_delta, "id", None)
        delta_id = tc_id or ""
        if isinstance(tc_id, int):  # Poolside sends integer ids
            tc_id = str(tc_id)

        self._active_slot_by_idx.setdefault(raw_idx, raw_idx)
        if (
            delta_id
            and raw_idx in self._last_id_at_idx
            and delta_id != self._last_id_at_idx[raw_idx]
        ):
            self._active_slot_by_idx[raw_idx] = max(self.acc, default=-1) + 1
        if delta_id:
            self._last_id_at_idx[raw_idx] = delta_id
        idx = self._active_slot_by_idx[raw_idx]

        entry = self.acc.setdefault(
            idx,
            {
                "id": tc_id or "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
                "extra_content": None,
            },
        )
        parts = self._argument_parts.setdefault(idx, [])
        if tc_id:
            entry["id"] = tc_id
        tc_function = getattr(tc_delta, "function", None)
        if tc_function:
            if getattr(tc_function, "name", None):
                # Assignment, not +=: names arrive complete and some providers (MiniMax via
                # NVIDIA NIM) resend the full name every chunk — += gives "read_fileread_file".
                entry["function"]["name"] = tc_function.name
            if getattr(tc_function, "arguments", None):
                parts.append(tc_function.arguments)
        extra = getattr(tc_delta, "extra_content", None)
        if extra is None and hasattr(tc_delta, "model_extra"):
            extra = (tc_delta.model_extra if isinstance(tc_delta.model_extra, dict) else {}).get(
                "extra_content"
            )
        if extra is not None:
            entry["extra_content"] = _dump_if_model(extra)
        name = entry["function"]["name"]
        if name and idx not in self._notified:
            self._notified.add(idx)
            return name  # type: ignore[no-any-return]  # entry names are str
        return None


class _StreamingCall(StreamingWaitMonitor):
    """One streaming request on the chat_completions wire.
    State shared between the request worker and the poll-loop monitor (interrupt abort) lives on
    the instance, mutated from both threads."""

    def __init__(self, agent: Any, api_kwargs: dict[str, Any]) -> None:
        self.agent = agent
        self.api_kwargs = api_kwargs
        self.worker: threading.Thread | None = None  # request thread
        self.result: dict[str, Any] = {"response": None, "error": None, "partial_tool_names": []}
        self.clients = _RequestClientRegistry(agent)
        # Request-local cancel flag: the worker recognizes its own interrupt
        # force-close (RemoteProtocolError) and exits instead of retrying (#6600).
        self._request_cancelled = {"value": False}
        self.deltas_were_sent = {"yes": False}  # for the partial-delivery fallback
        self.stream_attempt_lock = threading.Lock()
        self.stream_attempt_state: dict[str, Any] = {
            "current": 0,
            "cancelled": set(),
            "discarded_chunks": 0,
            "discarded_bytes": 0,
        }
        self.managed_stream_holder: dict[str, Any] = {"stream": None}
        # Per-attempt: request-local client.
        self._attempt_request_client: Any = None

    # ── shared small helpers ────────────────────────────────────────────

    @staticmethod
    def _quiet(fn: Callable[..., Any], *args: Any) -> None:
        """Best-effort callback: never let a display hook break the stream."""
        with contextlib.suppress(Exception):
            fn(*args)

    def _set_managed_stream(self, stream: Any) -> Any:
        self.managed_stream_holder["stream"] = stream
        return stream

    def _close_managed_stream(self) -> None:
        close = getattr(self.managed_stream_holder.pop("stream", None), "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("Managed provider stream cleanup failed", exc_info=True)

    def _start_stream_attempt(self) -> int:
        with self.stream_attempt_lock:
            self.stream_attempt_state["current"] += 1
            attempt_id = int(self.stream_attempt_state["current"])
        # Attempt-local: a tool name from a stream that died
        # before any text must not label a later attempt's partial stub or its retry decision.
        self.result["partial_tool_names"] = []
        return attempt_id

    def _cancel_current_stream_attempt(self, reason: str) -> None:
        with self.stream_attempt_lock:
            current = int(self.stream_attempt_state["current"])
            if current:
                self.stream_attempt_state["cancelled"].add(current)
        if current:
            logger.debug("Marked stream attempt %s cancelled: %s", current, reason)

    def _stream_attempt_is_active(self, stream_attempt_id: int) -> bool:
        with self.stream_attempt_lock:
            state = self.stream_attempt_state
            return (
                stream_attempt_id == int(state["current"])
                and stream_attempt_id not in state["cancelled"]
            )

    def _stream_attempt_was_cancelled(self, stream_attempt_id: int) -> bool:
        with self.stream_attempt_lock:
            return stream_attempt_id in self.stream_attempt_state["cancelled"]

    def _discard_stale_stream_chunk(self, stream_attempt_id: int, chunk: Any) -> None:
        try:
            chunk_bytes = len(repr(chunk))
        except Exception:
            chunk_bytes = 0
        with self.stream_attempt_lock:
            state = self.stream_attempt_state
            state["discarded_chunks"] += 1
            state["discarded_bytes"] += chunk_bytes
            discarded_chunks, discarded_bytes = state["discarded_chunks"], state["discarded_bytes"]
        first = discarded_chunks == 1
        (logger.warning if first else logger.debug)(
            (
                "Discarding chunk from superseded stream attempt %s "
                if first
                else "Discarded stale stream chunk from attempt %s "
            )
            + "(discarded_chunks=%s discarded_bytes=%s)",
            stream_attempt_id,
            discarded_chunks,
            discarded_bytes,
        )

    def _emit_text(self, text: str) -> None:
        self.agent._fire_stream_delta(text)
        self.deltas_were_sent["yes"] = True

    def _visible_text_delivered(self) -> bool:
        """True when visible assistant text actually reached a stream consumer this attempt
        (``_fire_stream_delta`` records only scrubbed, delivered text; ``deltas_were_sent``
        flips on any content delta, including whitespace/think-only ones nobody saw)."""
        return bool((getattr(self.agent, "_current_streamed_assistant_text", "") or "").strip())

    def _emit_tool_started(self, name: str) -> None:
        self.agent._fire_tool_gen_started(name)

    def _route_suppressed_text(self, text: str) -> None:
        """Tool-call turns suppress content streaming (no chatty preamble), but
        reasoning tags inside it must still reach the display: route through
        the delta callback for tag extraction (the CLI drops non-reasoning text
        once the stream box is closed)."""
        if self.agent.stream_delta_callback:
            self._quiet(
                lambda: (
                    self.agent.stream_delta_callback(text),
                    self.agent._record_streamed_assistant_text(text),
                )
            )

    # ── chat_completions wire ───────────────────────────────────────────

    def _stream_timeouts(self) -> tuple[float, float, float]:
        """``(write, read, connect/pool)`` socket timeouts: MERTINA_API_TIMEOUT (1800s) and
        MERTINA_STREAM_READ_TIMEOUT (120s); connect/pool cover the handshake, not
        inference: 30s."""
        base = env_float("MERTINA_API_TIMEOUT", 1800.0)
        read = env_float("MERTINA_STREAM_READ_TIMEOUT", 120.0)
        return base, read, 30.0

    @staticmethod
    def _choiceless_chunk(chunk: Any, finish_reason: Any) -> tuple[Any, Any]:
        """Chunk with empty ``choices`` -> ``(usage, finish_reason)``."""
        usage = (
            chunk.usage if hasattr(chunk, "usage") and chunk.usage else None
        )  # final usage chunk
        return usage, finish_reason

    def _open_chat_stream(self, stream_kwargs: dict[str, Any]) -> Any:
        # Strict endpoints that
        # already 4xx'd on it this session (``_stream_options_unsupported``, see #9705).
        if not getattr(self.agent, "_stream_options_unsupported", False):
            stream_kwargs["stream_options"] = {"include_usage": True}
        request_client = self._attempt_request_client = self.clients.set_client(
            self.agent._create_request_openai_client(reason="chat_completion_stream_request")
        )
        return request_client.chat.completions.create(**stream_kwargs)

    def _call_chat_completions(self, stream_attempt_id: int) -> Any:
        """Stream a chat completions response."""
        import httpx as _httpx

        base_timeout, read_timeout, conn_cap = self._stream_timeouts()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # OpenAI structured refusal (``delta.refusal``): the explanation streams here and
        # ``delta.content`` stays empty, so an un-accumulated refusal looks like an empty
        # stream and burns the empty-response retries (the non-streaming fix is #46013).
        refusal_parts: list[str] = []
        tool_calls = _ToolCallAccumulator()
        tool_calls_acc = tool_calls.acc
        finish_reason = model_name = usage_obj = None
        response_id = None  # the provider's own id, from the chunks
        role = "assistant"
        self._attempt_request_client = None

        def _open_stream(next_api_kwargs: dict[str, Any]) -> Any:
            timeout = _httpx.Timeout(
                connect=conn_cap, read=read_timeout, write=base_timeout, pool=conn_cap
            )
            return self._open_chat_stream({**next_api_kwargs, "stream": True, "timeout": timeout})

        stream = self._set_managed_stream(_open_stream(self.api_kwargs))

        for chunk in _iter_provider_stream_chunks(stream):
            if self.agent._interrupt_requested:
                # A half-read SSE response stays checked out of the httpx pool and the finally
                # would cache the client WITH the leaked connection: close on the owner first.
                try:
                    stream.close()
                except Exception:
                    # Still checked out: poison the slot so the finally really closes the pool.
                    if self._attempt_request_client is not None:
                        self.agent._abort_request_openai_client(
                            self._attempt_request_client, reason="interrupt_stream_close_failed"
                        )
                break
            if not self._stream_attempt_is_active(stream_attempt_id):
                self._discard_stale_stream_chunk(stream_attempt_id, chunk)
                continue
            if hasattr(chunk, "model") and chunk.model:
                model_name = chunk.model
            if response_id is None and isinstance(getattr(chunk, "id", None), str) and chunk.id:
                response_id = chunk.id
            if not chunk.choices:
                usage, finish_reason = self._choiceless_chunk(chunk, finish_reason)
                usage_obj = usage or usage_obj
                continue

            choice = chunk.choices[0]
            delta = choice.delta
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            if hasattr(chunk, "usage") and chunk.usage:
                usage_obj = chunk.usage

            reasoning_text = getattr(delta, "reasoning_content", None) or getattr(
                delta, "reasoning", None
            )
            # Same ``model_extra`` fallback as the non-streaming path: a reasoning-only stream
            # whose deltas carry only this field otherwise trips the empty-stream guard (#56516).
            if reasoning_text is None and isinstance(getattr(delta, "model_extra", None), dict):
                reasoning_text = delta.model_extra.get(
                    "reasoning_content"
                ) or delta.model_extra.get("reasoning")
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
            # Not routed to the live display: the transport promotes a sole-payload
            # refusal to content + ``content_filter`` and the loop surfaces it terminally.
            delta_refusal = getattr(delta, "refusal", None)
            if delta_refusal is None and isinstance(getattr(delta, "model_extra", None), dict):
                delta_refusal = delta.model_extra.get("refusal")
            if isinstance(delta_refusal, str) and delta_refusal:
                refusal_parts.append(delta_refusal)

            # Text (list-of-blocks deltas flattened once).
            delta_content = flatten_message_text(getattr(delta, "content", None), sep="")
            if delta_content:
                content_parts.append(delta_content)
                if tool_calls_acc:
                    self._route_suppressed_text(delta_content)
                else:
                    self._emit_text(delta_content)

            delta_tool_calls = getattr(delta, "tool_calls", None)
            if delta_tool_calls:
                for tc_delta in delta_tool_calls:
                    name = tool_calls.feed(tc_delta)
                    if name is not None:
                        self._emit_tool_started(name)
                        # Lets the stub-builder warn if streaming dies before the args
                        # complete instead of silently discarding the action.
                        self.result["partial_tool_names"].append(name)

        tool_calls.materialize()
        self._close_managed_stream()
        if self._stream_attempt_was_cancelled(stream_attempt_id):
            raise _httpx.RemoteProtocolError(f"stream attempt {stream_attempt_id} was superseded")
        return self._finish_chat_stream(
            role,
            content_parts,
            reasoning_parts,
            tool_calls_acc,
            finish_reason,
            model_name,
            usage_obj,
            response_id=response_id,
            refusal_parts=refusal_parts,
        )

    @staticmethod
    def _assemble_tool_calls(
        tool_calls_acc: dict[int, dict[str, Any]], finish_reason: Any
    ) -> tuple[list[SimpleNamespace] | None, bool]:
        """Materialize accumulated tool calls; flag truncated/unparseable args."""
        mock_tool_calls = []
        has_truncated_tool_args = False
        for idx in sorted(tool_calls_acc):
            tc = tool_calls_acc[idx]
            arguments = tc["function"]["arguments"]
            if arguments and arguments.strip():
                try:
                    json.loads(arguments)
                except json.JSONDecodeError:
                    has_truncated_tool_args = True
            elif finish_reason is None:
                # Name arrived, zero arg bytes, no finish_reason: unflagged this
                # becomes a "stop" turn executing "{}" with no retry.
                has_truncated_tool_args = True
            mock_tool_calls.append(
                SimpleNamespace(
                    id=tc["id"],
                    type=tc["type"],
                    extra_content=tc.get("extra_content"),
                    function=SimpleNamespace(name=tc["function"]["name"], arguments=arguments),
                )
            )
        return mock_tool_calls or None, has_truncated_tool_args

    def _finish_chat_stream(
        self,
        role: str,
        content_parts: list[str],
        reasoning_parts: list[str],
        tool_calls_acc: dict[int, dict[str, Any]],
        finish_reason: Any,
        model_name: Any,
        usage_obj: Any,
        *,
        response_id: str | None = None,
        refusal_parts: list[str] | None = None,
    ) -> SimpleNamespace:
        """Assemble the non-streaming-shaped response after the chunk loop. A
        stream ending with no finish_reason is a drop, not a completion: return a
        partial-stream stub so the loop fails fast instead of executing empty
        args or stamping "stop"."""
        full_content = "".join(content_parts) or None
        full_reasoning = "".join(reasoning_parts) or None
        mock_tool_calls, has_truncated_tool_args = self._assemble_tool_calls(
            tool_calls_acc, finish_reason
        )
        # Zero-chunk guard: nothing usable = upstream error / malformed SSE.
        if (
            finish_reason is None
            and not content_parts
            and not reasoning_parts
            and not refusal_parts
            and not tool_calls_acc
        ):
            raise EmptyStreamError(
                "Provider returned an empty stream with no finish_reason (possible upstream error or malformed SSE response)."  # noqa: E501  # upstream's message
            )
        if has_truncated_tool_args and finish_reason is None:
            # Partial args WITH finish_reason="length" is a real output cap; with NONE the
            # upstream dropped mid tool-call.
            _dropped_names = [
                (tool_calls_acc[idx]["function"]["name"] or "?") for idx in sorted(tool_calls_acc)
            ]
            logger.warning(
                "Stream ended with no finish_reason while a tool call's arguments were still incomplete "  # noqa: E501  # upstream's message
                "(tools=%s); treating as a mid-tool-call stream drop, not an output-length truncation.",  # noqa: E501  # upstream's message
                _dropped_names,
            )
            return _build_partial_stream_stub(
                role,
                full_content,
                full_reasoning,
                model_name,
                usage_obj,
            )
        if (
            finish_reason is None
            and (content_parts or reasoning_parts)
            and not tool_calls_acc
            and usage_obj is None
        ):
            # Text-only (or reasoning-only) drop: otherwise the partial text is stamped "stop".
            # A usage object proves the provider finished (include_usage's final chunk).
            logger.warning(
                "Stream ended with no finish_reason after delivering text with no tool calls; treating as a mid-stream drop."  # noqa: E501  # upstream's message
            )
            return _build_partial_stream_stub(
                role, full_content, full_reasoning, model_name, usage_obj
            )
        effective_finish_reason = "length" if has_truncated_tool_args else (finish_reason or "stop")
        message = SimpleNamespace(
            role=role,
            content=full_content,
            tool_calls=mock_tool_calls,
            reasoning_content=full_reasoning,
            # ``normalize_response`` reads ``message.refusal`` — same contract as the non-streaming
            # object.
            refusal="".join(refusal_parts or ()) or None,
        )
        # The provider's id when the chunks carried one (chatcmpl-/gen-...): it is what a provider
        # needs to look a request up. Fabricated only when the stream never sent one.
        response = SimpleNamespace(
            id=response_id or ("stream-" + str(uuid.uuid4())),
            model=model_name,
            usage=usage_obj,
            choices=[
                SimpleNamespace(index=0, message=message, finish_reason=effective_finish_reason)
            ],
        )
        return response

    # ── retry loop ──────────────────────────────────────────────────────

    def _retry_after_drop(self, reason: str) -> None:
        """Tear down the request-local client. Shared
        clients are never closed from inside a request (FD-recycle hazard); the
        OpenAI primary is replaced lazily."""
        self._cancel_current_stream_attempt(reason)
        self.clients.close_once(reason)

    def _handle_stream_error(self, e: Exception, attempt: int, max_retries: int) -> bool:
        """Classify a failed attempt: True = retry; False = stop with
        ``result["error"]`` set (unless our own interrupt force-closed the
        socket). Runs inside the ``except`` so ``logger.exception`` works."""
        import httpx as _httpx

        # Our own interrupt force-close: no retry/fallback/"reconnecting" (the
        # poll loop raises InterruptedError).
        if self._request_cancelled["value"]:
            logger.debug(
                "Streaming worker caught %s after request cancellation — exiting without retry.",
                type(e).__name__,
            )
            return False
        _is_timeout = isinstance(e, (_httpx.ReadTimeout, _httpx.ConnectTimeout, _httpx.PoolTimeout))
        # ReadError: abort/reset mid-body (ECONNRESET) — the retry loop owns recovery.
        _is_conn_err = isinstance(
            e, (_httpx.ConnectError, _httpx.ReadError, _httpx.RemoteProtocolError, ConnectionError)
        )
        _is_empty_stream = isinstance(e, EmptyStreamError)
        _is_sse_conn_err = not _is_timeout and not _is_conn_err and _is_sse_connection_error(e)
        _is_transient = _is_timeout or _is_conn_err or _is_sse_conn_err

        if (
            not self.deltas_were_sent["yes"]
            and not getattr(self.agent, "_stream_options_unsupported", False)
            and _rejects_stream_options(e)
        ):
            # Nothing streamed yet: drop the usage extension for this session and re-open.
            self.agent._stream_options_unsupported = True
            self._compat_retries = 1
            logger.info(
                "Endpoint rejected stream_options (HTTP %s); retrying without it for this session.",
                getattr(e, "status_code", None),
            )
            self._cancel_current_stream_attempt("stream_options_rejected_retry")
            self.clients.close_once("stream_options_rejected_retry")
            return True

        if self.deltas_were_sent["yes"]:
            _partial_tool_in_flight = bool(self.result.get("partial_tool_names"))
            if not _partial_tool_in_flight and not self._visible_text_delivered():
                # Deltas fired but nothing visible reached a consumer (whitespace/think-only
                # deltas, or no display consumer at all) and no tool call is in flight: from
                # the user's and the model's point of view NOTHING was delivered. The
                # "partial delivery" stub would be EMPTY (#112419). Classify as an
                # undelivered failure instead: same-prefix retry, then the main loop's
                # backoff — there is no text to duplicate.
                logger.warning(
                    "Stream died after deltas but before any visible text was delivered (0 chars, "
                    "no tool call in flight); treating as an undelivered stream failure: %s",
                    e,
                )
                self._quiet(self.agent._reset_stream_delivery_tracking)
                self.deltas_were_sent["yes"] = False
        if self.deltas_were_sent["yes"]:
            # Died AFTER tokens were delivered: normally no retry (would duplicate
            # text). Exception: a tool call in flight — aborting discards it, so
            # retry TRANSIENT errors (duplicated
            # preamble beats a failed action; no tool has executed yet).
            if not (_partial_tool_in_flight and _is_transient and attempt < max_retries):
                logger.warning("Streaming failed after partial delivery, not retrying: %s", e)
                self.result["error"] = e
                return False
            # Reset the streamed-text buffer so it isn't double-recorded; fresh accumulators.
            self._quiet(self.agent._reset_stream_delivery_tracking)
            self.deltas_were_sent["yes"] = False
            self._retry_after_drop(reason="stream_mid_tool_retry_cleanup")
            return True

        if _is_transient or _is_empty_stream:
            # Transient network / timeout error: retry with a fresh connection first.
            if attempt < max_retries:
                self._retry_after_drop(reason="stream_retry_cleanup")
                return True
            # Empty stream: "connection failed" would send users chasing network issues.
            if _is_empty_stream:
                _what = "Provider returned an empty response stream after"
                self.agent._buffer_diagnostic_status(
                    f"❌ {_what} {max_retries + 1} attempts. The provider may be experiencing issues — try again in a moment."  # noqa: E501  # upstream's message
                )
        else:
            logger.exception("Streaming failed before delivery: %s", e)
        # Propagate to the main retry loop (backoff).
        self.result["error"] = e
        return False

    def _call_wire(self, stream_attempt_id: int) -> Any:
        return self._call_chat_completions(stream_attempt_id)

    def _call(self) -> None:
        _max_stream_retries = env_int("MERTINA_STREAM_RETRIES", 2)
        # The one stream_options compatibility retry (#9705) is not a network retry and must not
        # consume the transient budget: on the last attempt (or MERTINA_STREAM_RETRIES=0) the
        # handler returned True and the loop ended with neither a response nor an error set.
        self._compat_retries = 0
        _stream_attempt = -1
        try:
            while _stream_attempt < _max_stream_retries + self._compat_retries:
                _stream_attempt += 1
                stream_attempt_id = self._start_stream_attempt()
                # Otherwise /stop closes the connection and the retry opens a
                # FRESH one, blocking up to a full read timeout per attempt.
                if self.agent._interrupt_requested:
                    self._cancel_current_stream_attempt("interrupt_before_stream_retry")
                    raise InterruptedError("Agent interrupted before stream retry")
                try:
                    self.result["response"] = _with_stream_emitters(
                        self.agent,
                        lambda: self._call_wire(stream_attempt_id),  # noqa: B023  # called before the loop moves on
                    )
                    return  # success
                except Exception as e:
                    self._close_managed_stream()
                    if not self._handle_stream_error(e, _stream_attempt, _max_stream_retries):
                        return
        except InterruptedError as e:
            # Fast pre-retry interrupt surfaces through the normal result channel.
            self.result["error"] = e
            return
        finally:
            self._close_managed_stream()
            # Reuse only after a clean stream; otherwise really close (fresh pool next).
            self.clients.close_once(
                "stream_request_complete"
                if self.result["response"] is not None
                else "stream_error_cleanup"
            )

    # ── poll-loop monitor (interrupt) ───────────────────────────────────

    def _run_call(self) -> None:
        try:
            self._call()
        finally:
            self._call_done.set()

    def _abort_for_interrupt(self) -> None:
        """/stop seen by the monitor: mark cancelled, abort the request-local
        socket, flag the interrupt."""
        # Mark cancelled BEFORE force-closing so the worker treats the forced
        # transport error as a cancel, not a network error (#6600).
        self._request_cancelled["value"] = True
        logger.debug("Force-closing streaming httpx client due to interrupt (not a network error).")
        with contextlib.suppress(Exception):
            self._cancel_current_stream_attempt("stream_interrupt_abort")
            # Only the request-local socket.
            self.clients.close_once("stream_interrupt_abort")
        self._monitor_interrupted["yes"] = True

    # ── orchestration ───────────────────────────────────────────────────

    def _partial_stream_stub(self) -> SimpleNamespace:
        """Tokens already reached the platform: a finish_reason="length" stub;
        tool_calls=None blocks executing incomplete calls.
        A text-only death with 0 visible chars never gets here: the error
        handler reclassifies it as undelivered (#112419)."""
        error = self.result["error"]
        _partial_text = (
            getattr(self.agent, "_current_streamed_assistant_text", "") or ""
        ).strip() or None
        _partial_names = list(self.result.get("partial_tool_names") or [])
        if _partial_names:
            # Warning so the user and model both know what was attempted.
            _name_str = ", ".join(_partial_names[:3])
            if len(_partial_names) > 3:
                _name_str += f", +{len(_partial_names) - 3} more"
            _warn = (
                f"\n\n⚠ Stream stalled mid tool-call ({_name_str}); the action was not executed. "
                f"Ask me to retry if you want to continue."
            )
            _partial_text = (_partial_text or "") + _warn  # model/result bookkeeping, never gated
            logger.warning(
                "Partial stream dropped tool call(s) %s after %s chars of text; surfaced warning to user: %s",  # noqa: E501  # upstream's message
                _partial_names,
                len(_partial_text or ""),
                error,
            )
        if not _partial_names:
            logger.warning(
                "Partial stream delivered before error; returning length-truncated stub with %s chars of "  # noqa: E501  # upstream's message
                "recovered content: %s",
                len(_partial_text or ""),
                error,
            )
        _stub = _build_partial_stream_stub(
            "assistant",
            _partial_text,
            None,
            getattr(self.agent, "model", "unknown"),
            None,
        )
        return _stub

    def run(self) -> Any:
        """Run the request on a worker thread,
        drive the interrupt monitor, then translate the outcome."""
        self._call_done = threading.Event()
        self._monitor_interrupted = {"yes": False}
        self.worker = threading.Thread(target=_context_thread_target(self._run_call), daemon=True)
        self.worker.start()
        self._monitor_loop()
        if self._monitor_interrupted["yes"]:
            raise InterruptedError("Agent interrupted during streaming API call")
        if self.agent._interrupt_requested:  # worker returned early before the monitor saw the flag
            raise InterruptedError("Agent interrupted during streaming API call (post-worker)")
        if self.result["error"] is not None:
            if self.deltas_were_sent["yes"]:
                return self._partial_stream_stub()
            raise self.result["error"]
        return self.result["response"]


def interruptible_streaming_api_call(agent: Any, api_kwargs: dict[str, Any]) -> Any:
    """Streaming variant of _interruptible_api_call: fires the delta callbacks per
    text token (tool-call turns suppress them) and returns a SimpleNamespace in
    the non-streaming response shape."""
    if agent._interrupt_requested:
        raise InterruptedError("Agent interrupted before streaming API call")
    return _StreamingCall(agent, api_kwargs).run()
