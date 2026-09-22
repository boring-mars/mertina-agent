# Ported from hermes-agent agent/agent_runtime_helpers.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Assorted AIAgent runtime helpers (message repair/sanitization, credential recovery, primary
runtime restore, prompt-cache policy, client construction, model switching, tool invocation).
Each function takes the parent ``AIAgent`` as ``agent`` except the stateless message helpers.
``_ra()`` resolves ``run_agent`` lazily so tests patching ``run_agent.X`` keep intercepting.
"""

from __future__ import annotations

import logging
import re
from types import ModuleType
from typing import Any

from mertina.agent.think_scrubber import THINK_TAG_NAMES

logger = logging.getLogger(__name__)


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
    r"(?:(?<=^)|(?<=[\n\r.!?:]))[ \t]*"
    r"<function\b[^>]*\bname\s*=[^>]*>"
    r"(?:(?:(?!</function>).)*)</function>",
    re.DOTALL | re.IGNORECASE,
)


_UNTERMINATED_REASONING_BLOCK_PATTERN = re.compile(
    rf"(?:^|\n)[ \t]*<(?:{'|'.join(THINK_TAG_NAMES)})\b[^>]*>.*$", re.DOTALL | re.IGNORECASE
)


_ORPHAN_REASONING_TAG_PATTERN = re.compile(
    rf"</?(?:{'|'.join(THINK_TAG_NAMES)})>\s*", re.IGNORECASE
)


_STRAY_TOOL_CALL_CLOSER_PATTERN = re.compile(
    rf"</(?:{_NS_PREFIX}(?:{'|'.join(_TOOL_CALL_TAG_NAMES)}|function))>\s*", re.IGNORECASE
)


# A tool-call opener with no closer, or GLM-style argument markup
# (<arg_key>/<arg_value>) outside any closed block, means the stream was
# cut mid-serialization of a text-channel tool call (#101899). The call
# can't be recovered; strip from the block-boundary opener (or the line
# holding the first stray argument tag) to the end of the text.
_UNTERMINATED_TOOL_CALL_PATTERN = re.compile(
    rf"(?:^|\n)[ \t]*<{_NS_PREFIX}(?:{'|'.join(_TOOL_CALL_TAG_NAMES)})\b[^>]*>.*$"
    r"|(?:^|\n)[^\n<]*</?arg_(?:key|value)\b.*$",
    re.DOTALL | re.IGNORECASE,
)


def _ra() -> ModuleType:
    """Lazy ``run_agent`` reference for test-patch routing."""
    from mertina import run_agent

    return run_agent


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
            if isinstance(part, str)
            or (
                isinstance(part, dict)
                and str(part.get("type") or "").strip().lower()
                not in {"thinking", "reasoning", "redacted_thinking"}
                and isinstance(part.get("text"), str)
                and part.get("text")
            )
        )
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


# Order matters: closed pairs first (case-insensitive so mixed-case tags don't fall through to the
# unterminated pass and eat trailing content), then tool-call XML blocks, the boundary+name-gated
# <function> block, the unterminated reasoning block, stray orphan reasoning tags, and finally stray
# tool-call CLOSERS only (bare/unterminated <function> is kept: a truncated streaming tail may still
# be valuable, matching OpenClaw's asymmetry).
_THINK_STRIP_PATTERNS = (
    *_REASONING_BLOCK_PATTERNS,
    *_TOOL_CALL_BLOCK_PATTERNS,
    _NAMED_FUNCTION_BLOCK_PATTERN,
    _UNTERMINATED_REASONING_BLOCK_PATTERN,
    _ORPHAN_REASONING_TAG_PATTERN,
    _STRAY_TOOL_CALL_CLOSER_PATTERN,
    _UNTERMINATED_TOOL_CALL_PATTERN,
)


def strip_think_blocks(agent, content: str) -> str:
    """Remove reasoning/thinking blocks from content, returning only visible text: closed tag
    pairs, unterminated open tags at a block boundary (mirrors ``gateway/stream_consumer.py``),
    stray orphan tags (all case-insensitive variants), and standalone tool-call XML blocks some
    open models emit; ``<function>`` is boundary- and ``name=``-gated so prose mentions survive."""
    content = _flatten_content_text(content) if content else ""
    for pattern in _THINK_STRIP_PATTERNS if content else ():
        content = pattern.sub("", content)
    return content


_INLINE_REASONING_PATTERNS = tuple(
    re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL | re.IGNORECASE) for tag in THINK_TAG_NAMES
)


def extract_reasoning(agent, assistant_message) -> str | None:
    """Reasoning text from ``reasoning`` / ``reasoning_content`` / ``reasoning_details``
    (OpenRouter unified), else inline thinking blocks in the content; None when absent."""
    parts: list[str] = []

    def _add(text) -> None:
        from mertina.agent.message_content import flatten_message_text

        text = flatten_message_text(text, sep="")
        if text and text not in parts:
            parts.append(text)

    _add(getattr(assistant_message, "reasoning", None))
    _add(getattr(assistant_message, "reasoning_content", None))
    # reasoning_details: [{"type": "reasoning.summary", "summary": "...", ...}, ...]
    for detail in getattr(assistant_message, "reasoning_details", None) or []:
        if isinstance(detail, dict):
            _add(
                detail.get("summary")
                or detail.get("thinking")
                or detail.get("content")
                or detail.get("text")
            )
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


def create_openai_client(
    agent: Any, client_kwargs: dict[str, Any], *, reason: str, shared: bool
) -> Any:
    # Treat client_kwargs as read-only: callers pass agent._client_kwargs, and in-place mutation
    # leaks into later requests (a torn-down httpx transport got reused).
    # Callers pass agent._client_kwargs (or shallow copies of it) in; any in-place mutation leaks
    # back into the stored dict and is reused on subsequent requests. #10933 hit this by injecting
    # an httpx.Client transport that was torn down after the first request, so the next request
    # wrapped a closed transport and raised "Cannot send a request, as the client has been closed"
    # on every retry. The revert resolved that specific path; this copy locks the contract so future
    # transport/keepalive work can't reintroduce the same class of bug.
    client_kwargs = dict(client_kwargs)
    # Retries belong to the outer conversation loop (honors Retry-After); SDK retries would
    # double-retry inside it. auxiliary_client keeps SDK retries as it isn't wrapped.
    # Delegate all rate-limit / 5xx retry to hermes's outer conversation loop, which honors
    # Retry-After and applies adaptive/jittered backoff. The OpenAI SDK default (max_retries=2) uses
    # its own 1-2s backoff that ignores Retry-After and double-retries inside our loop — the same
    # deadlock the Anthropic clients hit (#26293). This is the single chokepoint every primary
    # OpenAI/aggregator client passes through (init, switch_model, recovery, restore,
    # request-scoped); auxiliary_client builds its own clients and keeps SDK retries because it is
    # NOT wrapped by the conversation loop.
    client_kwargs.setdefault("max_retries", 0)
    # ``process_bootstrap.OpenAI`` is a lazy SDK proxy; resolved at call time so tests can patch it.
    from mertina.agent import process_bootstrap

    client = process_bootstrap.OpenAI(**client_kwargs)
    _ra().logger.info(
        "OpenAI client created (%s, shared=%s) %s", reason, shared, agent._client_log_context()
    )
    return client


def invoke_tool(
    agent: Any,
    function_name: str,
    function_args: dict[str, Any],
    effective_task_id: str,
    tool_call_id: str | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> str:
    """Invoke a single registry-dispatched tool and return the result string; no display
    logic. Used by the concurrent path; the sequential path keeps its own inline invocation."""
    if not isinstance(function_args, dict):
        function_args = {}  # type: ignore[unreachable]  # models can send non-object arguments

    def _execute(next_args: dict[str, Any]) -> Any:
        dispatch_kwargs = dict(  # noqa: C408  # upstream's form
            tool_call_id=tool_call_id,
            session_id=agent.session_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
        )
        from mertina import model_tools

        return model_tools.handle_function_call(
            function_name, next_args, effective_task_id, **dispatch_kwargs
        )

    return _execute(function_args)  # type: ignore[no-any-return]  # upstream types _execute as Any


def copy_reasoning_content_for_api(agent, source_msg: dict, api_msg: dict) -> None:
    """Forward reasoning fields onto an API replay message; policy lives in ``agent.message_sanitization.apply_reasoning_content_policy``."""
    from mertina.agent.message_sanitization import apply_reasoning_content_policy

    apply_reasoning_content_policy(source_msg, api_msg, agent._needs_thinking_reasoning_pad())
