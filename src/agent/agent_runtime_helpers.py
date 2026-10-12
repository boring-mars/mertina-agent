"""Assorted AIAgent runtime helpers (message repair/sanitization, credential recovery, primary
runtime restore, prompt-cache policy, client construction, model switching, tool invocation).
Each function takes the parent ``AIAgent`` as ``agent`` except the stateless message helpers.
``_ra()`` resolves ``run_agent`` lazily so tests patching ``run_agent.X`` keep intercepting.
"""

from __future__ import annotations

import contextlib
import re

from agent.think_scrubber import THINK_TAG_NAMES

from typing import Any, Dict, List, Optional, Tuple





def _ra():
    """Lazy ``run_agent`` reference for test-patch routing."""
    import run_agent
    return run_agent


def create_openai_client(agent, client_kwargs: dict, *, reason: str, shared: bool) -> Any:
    # Treat client_kwargs as read-only: callers pass agent._client_kwargs, and in-place mutation
    # leaks into later requests (a torn-down httpx transport got reused).
    # Callers pass agent._client_kwargs (or shallow copies of it) in; any in-place mutation leaks back into
    # the stored dict and is reused on subsequent requests. #10933 hit this by injecting an httpx.Client
    # transport that was torn down after the first request, so the next request wrapped a closed transport
    # and raised "Cannot send a request, as the client has been closed" on every retry. The revert resolved
    # that specific path; this copy locks the contract so future transport/keepalive work can't reintroduce
    # the same class of bug.
    client_kwargs = dict(client_kwargs)
    # Retries belong to the outer conversation loop (honors Retry-After); SDK retries would
    # double-retry inside it. auxiliary_client keeps SDK retries as it isn't wrapped.
    # Delegate all rate-limit / 5xx retry to hermes's outer conversation loop, which honors Retry-After and
    # applies adaptive/jittered backoff. The OpenAI SDK default (max_retries=2) uses its own 1-2s backoff
    # that ignores Retry-After and double-retries inside our loop — the same deadlock the Anthropic clients
    # hit (#26293). This is the single chokepoint every primary OpenAI/aggregator client passes through
    # (init, switch_model, recovery, restore, request-scoped); auxiliary_client builds its own clients and
    # keeps SDK retries because it is NOT wrapped by the conversation loop.
    client_kwargs.setdefault("max_retries", 0)
    # ``process_bootstrap.OpenAI`` is a lazy SDK proxy; resolved at call time so tests can patch it.
    from agent import process_bootstrap
    client = process_bootstrap.OpenAI(**client_kwargs)
    return client


_INTERRUPTED_PLACEHOLDER = "[response interrupted]"


def _content_has_payload(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return content not in (None, "")
    # Any typed block counts, as long as a text block is not itself blank.
    return any(
        (block.get("type") != "text" or (isinstance(block.get("text"), str) and block["text"].strip()))
        if isinstance(block, dict) else bool(block)
        for block in content
    )


def _msg_has_payload(msg: Dict[str, Any]) -> bool:
    """True if ``msg`` carries anything the API treats as non-empty content (text, multimodal
    blocks, tool_calls, reasoning). Role-agnostic counterpart of ``AIAgent._is_thinking_only_assistant``.
    Codex Responses item carriers persist with content:"" by design (text lives in codex_*_items
    and is replayed); treating them as payload keeps the repair from rewriting a designed-empty turn."""
    return _content_has_payload(msg.get("content")) or bool(
        msg.get("tool_calls")
        or (isinstance(msg.get("reasoning_content"), str) and msg["reasoning_content"].strip())
        or msg.get("reasoning")
        or msg.get("reasoning_details")
        or msg.get("codex_message_items")
        or msg.get("codex_reasoning_items")
    )


def fill_empty_non_final_wire_payload(msg: Dict[str, Any], *, is_final: bool) -> bool:
    """Write the interrupted placeholder onto an empty non-final wire copy; True when filled.
    Pass the per-call copy only; durable history must not be mutated."""
    if is_final or not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
        return False
    if _msg_has_payload(msg):
        return False
    msg["content"] = _INTERRUPTED_PLACEHOLDER
    return True


# Cap same-entry OAuth refreshes on a persistent auth failure, else a single-entry pool re-mints forever.
_MAX_AUTH_REFRESH_ATTEMPTS = 2
_TOOL_CALL_TAG_NAMES = ("tool_call", "tool_calls", "tool_result", "function_call", "function_calls")

# Optional XML namespace prefix: some models serialize native tool calls as <ns:function_calls>.
_NS_PREFIX = r"(?:[\w.-]+:)?"
_REASONING_BLOCK_PATTERNS = tuple(
    re.compile(rf"<{name}>.*?</{name}>", re.DOTALL | re.IGNORECASE) for name in THINK_TAG_NAMES
)
_TOOL_CALL_BLOCK_PATTERNS = tuple(
    re.compile(rf"<{_NS_PREFIX}{name}\b[^>]*>.*?</{_NS_PREFIX}{name}>", re.DOTALL | re.IGNORECASE)
    for name in _TOOL_CALL_TAG_NAMES
)

# Named <function name=...> blocks; boundary- and name-gated (see _THINK_STRIP_PATTERNS note).
_NAMED_FUNCTION_BLOCK_PATTERN = re.compile(
    r'(?:(?<=^)|(?<=[\n\r.!?:]))[ \t]*'
    r'<function\b[^>]*\bname\s*=[^>]*>'
    r'(?:(?:(?!</function>).)*)</function>', re.DOTALL | re.IGNORECASE,
)
_UNTERMINATED_REASONING_BLOCK_PATTERN = re.compile(
    rf'(?:^|\n)[ \t]*<(?:{"|".join(THINK_TAG_NAMES)})\b[^>]*>.*$', re.DOTALL | re.IGNORECASE
)
_ORPHAN_REASONING_TAG_PATTERN = re.compile(
    rf'</?(?:{"|".join(THINK_TAG_NAMES)})>\s*', re.IGNORECASE
)
_STRAY_TOOL_CALL_CLOSER_PATTERN = re.compile(
    rf'</(?:{_NS_PREFIX}(?:{"|".join(_TOOL_CALL_TAG_NAMES)}|function))>\s*', re.IGNORECASE
)

# A tool-call opener with no closer, or GLM-style argument markup
# (<arg_key>/<arg_value>) outside any closed block, means the stream was
# cut mid-serialization of a text-channel tool call (#101899). The call
# can't be recovered; strip from the block-boundary opener (or the line
# holding the first stray argument tag) to the end of the text.
_UNTERMINATED_TOOL_CALL_PATTERN = re.compile(
    rf'(?:^|\n)[ \t]*<{_NS_PREFIX}(?:{"|".join(_TOOL_CALL_TAG_NAMES)})\b[^>]*>.*$'
    r'|(?:^|\n)[^\n<]*</?arg_(?:key|value)\b.*$',
    re.DOTALL | re.IGNORECASE,
)


def _flatten_content_text(content: Any) -> str:
    """Flatten list/dict content (e.g. Anthropic-via-OpenRouter block lists) to text: a raw list
    hitting ``re.sub`` raises TypeError and the loop retries forever. Thinking/reasoning blocks
    are dropped outright; their text key varies per provider."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else part.get("text")
            for part in content
            if isinstance(part, str) or (
                isinstance(part, dict)
                and str(part.get("type") or "").strip().lower() not in {"thinking", "reasoning", "redacted_thinking"}
                and isinstance(part.get("text"), str) and part.get("text")
            )
        )
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)

# unterminated pass and eat trailing content), then tool-call XML blocks, the boundary+name-gated
# <function> block, the unterminated reasoning block, stray orphan reasoning tags, and finally stray
# tool-call CLOSERS only (bare/unterminated <function> is kept: a truncated streaming tail may still
# be valuable, matching OpenClaw's asymmetry).
_THINK_STRIP_PATTERNS = (
    *_REASONING_BLOCK_PATTERNS, *_TOOL_CALL_BLOCK_PATTERNS, _NAMED_FUNCTION_BLOCK_PATTERN,
    _UNTERMINATED_REASONING_BLOCK_PATTERN, _ORPHAN_REASONING_TAG_PATTERN,
    _STRAY_TOOL_CALL_CLOSER_PATTERN, _UNTERMINATED_TOOL_CALL_PATTERN,
)

def strip_think_blocks(agent, content: str) -> str:
    """Remove reasoning/thinking blocks from content, returning only visible text: closed tag
    pairs, unterminated open tags at a block boundary (mirrors ``gateway/stream_consumer.py``),
    stray orphan tags (all case-insensitive variants), and standalone tool-call XML blocks some
    open models emit; ``<function>`` is boundary- and ``name=``-gated so prose mentions survive."""
    content = _flatten_content_text(content) if content else ""
    for pattern in _THINK_STRIP_PATTERNS if content else ():
        content = pattern.sub('', content)
    return content

_INLINE_REASONING_PATTERNS = tuple(
    re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL | re.IGNORECASE)
    for tag in THINK_TAG_NAMES
)

def extract_reasoning(agent, assistant_message) -> Optional[str]:
    """Reasoning text from ``reasoning`` / ``reasoning_content`` / ``reasoning_details``
    (OpenRouter unified), else inline thinking blocks in the content; None when absent."""
    parts: List[str] = []

    def _add(text) -> None:
        from agent.message_content import flatten_message_text

        text = flatten_message_text(text, sep="")
        if text and text not in parts:
            parts.append(text)
    _add(getattr(assistant_message, "reasoning", None))
    _add(getattr(assistant_message, "reasoning_content", None))
    # reasoning_details: [{"type": "reasoning.summary", "summary": "...", ...}, ...]
    for detail in getattr(assistant_message, "reasoning_details", None) or []:
        if isinstance(detail, dict):
            _add(detail.get('summary') or detail.get('thinking') or detail.get('content') or detail.get('text'))
    # Fall back to reasoning embedded in content only when no structured field was found.
    content = getattr(assistant_message, "content", None)
    if not parts and isinstance(content, list):
        # DeepSeek V4 Pro returns typed content blocks ({"type": "thinking", ...}); dropping them
        # makes the next turn fail with HTTP 400 "thinking must be passed back".
        # Refs #21944.
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                _add((block.get("thinking") or block.get("text") or "").strip())
    if not parts and isinstance(content, str) and content:
        for pattern in _INLINE_REASONING_PATTERNS:
            for block in pattern.findall(content):
                _add(block.strip())
    return "\n\n".join(parts) if parts else None


def _iter_httpx_pools_with_owner(http_client: Any):
    """Yield ``(pool, owner)`` pairs reachable from an httpx client, including mounted transports:
    keepalive and proxy configs put live connections on ``client._mounts``, which a
    ``_transport``-only walk misses.

    ``owner`` is ``None`` for a pool this client owns outright, or the ``_SharedTransport`` view
    id when the pool is process-shared with other clients
    (``process_bootstrap.build_keepalive_http_client``). Callers must then touch only the
    in-flight requests stamped with that owner.

    Walking the default transport alone makes ``force_close_tcp_sockets`` return 0 while a stream is still
    mid-recv — the interrupt logs success and the provider keeps burning the slot (#72975).
    """
    seen_pools: set[int] = set()
    try:
        transports = [getattr(http_client, "_transport", None)]
        transports += list((getattr(http_client, "_mounts", None) or {}).values())
        for transport in transports:
            if transport is None:
                continue
            # Connections live under ``_pool``; a directly mounted HTTPProxy *is* a ConnectionPool,
            # so ``_connections`` may sit on the transport itself.
            pool = getattr(transport, "_pool", None)
            if pool is None and getattr(transport, "_connections", None) is not None:
                pool = transport
            if pool is not None and id(pool) not in seen_pools:
                seen_pools.add(id(pool))
                owner = id(transport) if type(transport).__name__ == "_SharedTransport" else None
                yield pool, owner
    except Exception:
        return


def _connection_candidates(conn: Any):
    """Walk nested wrappers: proxy tunnels (``_connection``) plus httpx/httpcore
    stream envelopes (``_stream``/``_httpcore_stream``: BoundSyncStream →
    ResponseStream → connection byte stream → HTTP11/2 connection)."""
    seen: set[int] = set()
    stack = [conn]
    while stack:
        obj = stack.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        yield obj
        for attr in ("_connection", "_stream", "_httpcore_stream"):
            nxt = getattr(obj, attr, None)
            if nxt is not None:
                stack.append(nxt)


def _socket_from_candidate(candidate: Any):
    """Raw socket behind a connection/stream wrapper yielded by ``_connection_candidates``."""
    stream = getattr(candidate, "_network_stream", None) or getattr(candidate, "_stream", None)
    sock = _socket_from_stream(stream) if stream is not None else None
    return sock if sock is not None else _socket_from_stream(candidate)


def _socket_from_stream(stream: Any):
    """Raw socket behind an httpcore network stream (several backends), or None."""
    sock = getattr(stream, "_sock", None)
    if sock is None and callable(getattr(stream, "get_extra_info", None)):
        with contextlib.suppress(Exception):
            sock = stream.get_extra_info("socket")
    if sock is None:
        sock = getattr(getattr(stream, "stream", None), "_sock", None)
    if sock is None and callable(getattr(getattr(stream, "_stream", None), "extra", None)):
        # anyio-backed streams expose the raw socket through SocketAttribute.raw_socket.
        with contextlib.suppress(Exception):
            from anyio.abc import SocketAttribute
            sock = stream._stream.extra(SocketAttribute.raw_socket)
    return sock


def _iter_pool_sockets(client: Any):
    """Yield raw sockets reachable from an OpenAI/httpx client pool. Defensive over private
    httpcore internals (``conn._connection``, proxy tunnel wrappers) that vary by release; also
    walks mount transports and in-flight ``PoolRequest.connection`` objects (``_connections``
    is empty during checkout)."""
    try:
        # Some SDK wrappers *are* the httpx client; fall through so mount-aware discovery runs.
        http_client = getattr(client, "_client", None)
        pools = list(_iter_httpx_pools_with_owner(client if http_client is None else http_client))
    except Exception:
        return
    if not pools:
        return
    from agent.process_bootstrap import HERMES_TRANSPORT_OWNER_EXT
    seen: set[int] = set()
    for pool, owner in pools:
        # ``is None``, not falsiness: an empty ``_connections`` must still let us walk in-flight ``_requests``.
        raw_conns = getattr(pool, "_connections", None)
        if raw_conns is None:
            raw_conns = getattr(pool, "_pool", None)
        # A process-shared pool carries other clients' idle + in-flight connections: only this
        # client's own in-flight requests (stamped by ``_SharedTransport.handle_request``) may be
        # shut down.
        connections = [] if owner is not None else list(raw_conns or [])
        for pool_req in list(getattr(pool, "_requests", None) or []):
            if owner is not None:
                exts = getattr(getattr(pool_req, "request", None), "extensions", None) or {}
                if exts.get(HERMES_TRANSPORT_OWNER_EXT) != owner:
                    continue
            conn = getattr(pool_req, "connection", None)
            if conn is not None:
                connections.append(conn)
        for conn in connections:
            for candidate in _connection_candidates(conn):
                sock = _socket_from_candidate(candidate)
                if sock is not None and id(sock) not in seen:
                    seen.add(id(sock))
                    yield sock


def _shutdown_socket(sock: Any) -> None:
    """``shutdown(SHUT_RDWR)`` WITHOUT closing the FD. ``close()`` from a non-owner thread is
    unsafe: the SSL BIO caches the raw FD, the kernel recycles it, and a flushed TLS record lands
    in the wrong file (once clobbered a SQLite header). ``shutdown()`` is FD-safe from any thread.
    Already shut down / not connected / FD invalid are all benign."""
    import socket as _socket
    try:
        # Clear a blocking timeout so a hung SSL_read notices the shutdown. Still no close().
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            with contextlib.suppress(OSError):
                settimeout(0)
        sock.shutdown(_socket.SHUT_RDWR)
    except OSError:
        pass


def force_close_tcp_sockets(client: Any) -> int:
    """Abort in-flight TCP I/O on every pool socket via ``_shutdown_socket``. Returns the count
    (logged as ``tcp_force_closed=N``)."""
    shutdown_count = 0
    try:
        for sock in _iter_pool_sockets(client):
            _shutdown_socket(sock)
            shutdown_count += 1
    except Exception as exc:
        _ra().logger.debug("Force-close TCP sockets sweep error: %s", exc)
    return shutdown_count
