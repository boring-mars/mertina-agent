# Ported from hermes-agent agent/chat_completion_helpers.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""API-call helpers extracted from :class:`AIAgent`: the non-streaming request driver, request
kwargs builder, assistant-message materializer and max-iterations handler.

Each function takes the parent ``AIAgent`` as ``agent``; AIAgent keeps thin forwarders.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from mertina.agent.message_content import flatten_message_text
from mertina.agent.message_metadata import append_message, stamp_message_timestamp
from mertina.agent.message_sanitization import (
    _sanitize_surrogates,
)
from mertina.agent.sdk_transform_bypass import bypass_chat_sdk_request_transform
from mertina.agent.transports.base import ProviderTransport
from mertina.agent.transports.types import NormalizedResponse
from mertina.utils import env_int

logger = logging.getLogger(__name__)


_IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})


def _image_part_chars(part: dict[str, Any], image_cost: int) -> int:
    """Char-equivalent of one image content part: the per-image cost learned from provider usage
    (x4 chars/token), never the base64 payload length. A single native screenshot priced as text
    read as ~100K+ tokens and selected the giant-conversation watchdog tiers (#63871, #76411)."""
    text = part.get("text")
    return image_cost * 4 + (len(text) if isinstance(text, str) else 0)


def _payload_chars(value: Any, image_cost: int) -> int:
    """``len(str(value))`` with image content parts priced at ``image_cost`` tokens each."""
    if value is None:
        return 0
    if isinstance(value, dict):
        part_type = value.get("type")
        # JSON-Schema nodes may hold a sub-schema (``properties.type``) or a multi-type list
        # under the "type" key; only scalar content-part types can ever match (#104793).
        if (
            isinstance(part_type, str)
            and part_type in _IMAGE_PART_TYPES
            and any(k in value for k in ("image_url", "image", "source", "file_id"))
        ):
            return _image_part_chars(value, image_cost)
        return sum(len(str(k)) + 6 + _payload_chars(v, image_cost) for k, v in value.items())
    if isinstance(value, list):
        return sum(_payload_chars(item, image_cost) for item in value) + 2 * len(value)
    return len(str(value))


def estimate_request_context_tokens(api_payload: Any) -> int:
    """Cheap char/4 context estimate for the stale-call detectors. Handles both
    wire shapes so Codex turns don't report ~0 tokens: list -> Chat ``messages``;
    dict with ``messages`` (+``tools``); dict with ``input`` (Responses API,
    +``instructions``/``tools``); any other dict -> sum of its values. Image parts
    cost the learned per-image price, not their base64 length."""
    from mertina.agent.image_token_cost import current_image_token_cost

    image_cost = current_image_token_cost()

    def _chars(value: Any) -> int:
        return _payload_chars(value, image_cost)

    if isinstance(api_payload, list):
        return sum(_chars(item) for item in api_payload) // 4
    if not isinstance(api_payload, dict):
        return _chars(api_payload) // 4
    messages = api_payload.get("messages")
    if isinstance(messages, list):
        total_chars = sum(_chars(item) for item in messages)
        if "tools" in api_payload:
            total_chars += _chars(api_payload.get("tools"))
        return total_chars // 4
    if "input" in api_payload:
        return sum(_chars(api_payload.get(k)) for k in ("input", "instructions", "tools")) // 4
    return sum(_chars(value) for value in api_payload.values()) // 4


def _stale_streak(agent) -> int:
    try:
        return int(getattr(agent, "_consecutive_stale_streams", 0) or 0)
    except Exception:
        return 0


def _bump_stale_streak(agent) -> None:
    with contextlib.suppress(Exception):
        agent._consecutive_stale_streams = _stale_streak(agent) + 1


def _reset_stale_streak(agent) -> None:
    with contextlib.suppress(Exception):
        agent._consecutive_stale_streams = 0


def _report_stale_nonstream_kill(
    agent,
    api_kwargs: dict,
    elapsed: float,
    stale_timeout: float,
    *,
    inline: bool = False,
    hint: str | None = None,
) -> None:
    """Log + status message for a stale non-streaming kill, shared by the worker
    poll loop and the inline ``direct_api_call`` watchdog (their kill/state
    sequences differ deliberately: different locking models)."""
    model = api_kwargs.get("model", "unknown")
    logger.warning(
        "%son-streaming API call stale for %.0fs (threshold %.0fs). "
        "model=%s context=~%s tokens. Killing connection.",
        "Inline n" if inline else "N",
        elapsed,
        stale_timeout,
        model,
        f"{estimate_request_context_tokens(api_kwargs):,}",
    )
    try:
        agent._buffer_diagnostic_status(
            f"⚠️ No response from provider for {int(elapsed)}s (non-streaming, model: {model}). {hint or 'Aborting call.'}"
        )
    except Exception:
        logger.debug("stale status buffering failed", exc_info=True)


def _touch_stale_kill_activity(agent, elapsed: float) -> None:
    try:
        agent._touch_activity(f"stale non-streaming call killed after {int(elapsed)}s")
    except Exception:
        logger.debug("stale activity touch failed", exc_info=True)


def _check_stale_giveup(agent) -> None:
    """Raise immediately when the consecutive-stale streak is past the
    give-up threshold — no network attempt, no stale-timeout wait."""
    _giveup = env_int("MERTINA_STREAM_STALE_GIVEUP", 5)
    _streak = _stale_streak(agent)
    if _giveup > 0 and _streak >= _giveup:
        raise RuntimeError(
            "Provider has been unresponsive (no response received) for "
            f"{_streak} consecutive stale attempts — aborting this call to "
            "avoid an indefinite stall. Switch models or start a new session, then retry."
        )


def _bedrock_converse_call(api_kwargs: dict, *, stream: bool, on_stream_denied=None):
    """Pop the Mertina routing keys and call ``converse`` / ``converse_stream`` (boto3
    directly) with the shared recovery: a cachePoint rejection (Nova: toolConfig.tools,
    #97281) drops the marker and resends once inside the same attempt; a streaming IAM
    denial hands off to ``on_stream_denied(client, kwargs, exc)``; a stale connection
    evicts the cached client so the outer retry builds a fresh pool. Streaming returns the
    event stream; non-streaming an OpenAI-shaped SimpleNamespace."""
    from mertina.agent.bedrock_adapter import (
        _get_bedrock_runtime_client,
        invalidate_runtime_client,
        is_stale_connection_error,
        is_streaming_access_denied_error,
        normalize_converse_response,
        recover_from_cache_point_rejection,
    )

    region = api_kwargs.pop("__bedrock_region__", "us-east-1")
    api_kwargs.pop("__bedrock_converse__", None)
    client = _get_bedrock_runtime_client(region)
    method = client.converse_stream if stream else client.converse
    finish = (lambda raw: raw.get("stream", [])) if stream else normalize_converse_response
    try:
        raw_response = method(**api_kwargs)
    except Exception as exc:
        retry_kwargs = recover_from_cache_point_rejection(exc, api_kwargs)
        if retry_kwargs is not None:
            return finish(method(**retry_kwargs))
        if on_stream_denied is not None and is_streaming_access_denied_error(exc):
            return on_stream_denied(client, api_kwargs, exc)
        if is_stale_connection_error(exc):
            invalidate_runtime_client(region)
        raise
    return finish(raw_response)


def _dispatch_nonstreaming_api_request(agent, api_kwargs: dict, *, make_client):
    """Run one non-streaming LLM request for the active api_mode and return it.

    Shared by ``interruptible_api_call`` and ``direct_api_call``. ``make_client(reason,
    kind=...)`` builds the per-request client (``"openai"`` / ``"anthropic_messages"``)
    so callers can register it with their abort/close machinery; bedrock / MoA
    manage their own clients. Interrupt/abort/close semantics stay in callers.
    """
    if agent.api_mode == "codex_responses":
        return agent._run_codex_stream(
            api_kwargs,
            client=make_client("codex_stream_request"),
            on_first_delta=getattr(agent, "_codex_on_first_delta", None),
        )
    if agent.api_mode == "anthropic_messages":
        # Request-local client so the stale/interrupt watchdog aborts sockets
        # from the stranger thread while the worker owns the SDK close (#67142).
        request_client = make_client("anthropic_messages_request", kind="anthropic_messages")
        return agent._anthropic_messages_create(api_kwargs, client=request_client)
    if agent.api_mode == "bedrock_converse":
        return _bedrock_converse_call(api_kwargs, stream=False)
    if agent.provider == "moa":
        # MoA is a virtual provider backed by the in-process MoAClient facade — never
        # rebuild a request-local client from the virtual metadata. After a client
        # replacement agent.client may be a native OpenAI client while provider stays
        # "moa": pop the MoA-internal key ONLY then (the facade consumes it; stripping
        # it there forces a duplicate fan-out). Only the facade exposes ``prepare()`` (#78382).
        _completions = getattr(getattr(agent.client, "chat", None), "completions", None)
        if not callable(getattr(_completions, "prepare", None)):
            api_kwargs.pop("_moa_prepared_request", None)
        return agent.client.chat.completions.create(**api_kwargs)
    request_client = make_client("chat_completion_request")
    # #93650: keep the bulk wire-format payload out of the SDK's GIL-holding
    # request transform. No-op unless this really is the OpenAI SDK, so the
    # MoA facade above and the suite's stand-in clients are unaffected.
    api_kwargs = bypass_chat_sdk_request_transform(api_kwargs, request_client)
    return request_client.chat.completions.create(**api_kwargs)


# How often an in-flight direct_api_call refreshes last_activity_ts. Must stay well
# under the async-delegation idle stall threshold (450s) and below the 30s monitor sweep.
_DIRECT_API_ACTIVITY_HEARTBEAT_SECONDS = 15.0


def _resolve_direct_stale_timeout(agent, api_kwargs: dict) -> float:
    """Stale budget for the inline call via ``agent._compute_non_stream_stale_timeout``.
    A non-numeric result (stub agent) leaves the watchdog disarmed; a resolver
    that *raises* propagates — swallowing into ``inf`` would reinstate the hang."""
    resolver = getattr(agent, "_compute_non_stream_stale_timeout", None)
    value = resolver(api_kwargs) if callable(resolver) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("inf")
    return float(value)


def _inline_nonstream_hard_timeout(stale_timeout: float):
    """Socket-level backstop for inline non-streaming calls (#85252): the keepalive
    client uses ``read=None`` and the stranger-thread abort must not ``close()`` the
    FD (#29507), so a hung provider otherwise waits until TCP dies. Returns an
    ``httpx.Timeout`` with read == stale budget, a float if httpx is unavailable,
    or ``None`` when the watchdog is disarmed (non-finite budget)."""
    if not math.isfinite(stale_timeout) or stale_timeout <= 0:
        return None
    conn_cap = min(stale_timeout, 60.0)
    try:
        import httpx as _httpx

        return _httpx.Timeout(connect=conn_cap, read=stale_timeout, write=conn_cap, pool=conn_cap)
    except Exception:
        return stale_timeout


class _InlineRequest:
    """Lifecycle state for one inline non-streaming request (#75301). Every transition
    happens under ``lock``: ``done`` stops a late timer bumping the stale streak after
    unwind; ``cancelled`` lets an interrupt own the outcome so a racing timer can't
    misclassify the kill as staleness; ``stale`` is the one-shot transition."""

    def __init__(self, agent, api_kwargs: dict, stale_timeout: float, call_start: float):
        self.agent = agent
        self.api_kwargs = api_kwargs
        self.stale_timeout = stale_timeout
        self.call_start = call_start
        self.client = None
        self.done = False
        self.stale = False
        self.cancelled = False
        self.lock = threading.Lock()
        self.abort_hook = self.abort  # single bound object: identity-checked on cleanup
        self._hb_stop = threading.Event()
        self._hb = threading.Thread(
            target=self._activity_heartbeat, name="direct-api-activity-hb", daemon=True
        )
        self._watchdog = None

    def _activity_heartbeat(self) -> None:
        # Never put the API call itself on another worker thread — that is the nested-pool
        # deadlock this path exists to avoid (#60203). This ticker only refreshes the clock.
        while not self._hb_stop.wait(_DIRECT_API_ACTIVITY_HEARTBEAT_SECONDS):
            with contextlib.suppress(Exception):
                self.agent._touch_activity("waiting for non-streaming API response")

    def _on_stale(self) -> None:
        # Timer thread: aborts sockets only, never issues a request (keeps the no-worker
        # property). False = request finished or an interrupt owns the outcome; stay silent.
        if not self.abort("stale_call_kill"):
            return
        elapsed = time.time() - self.call_start
        _report_stale_nonstream_kill(
            self.agent, self.api_kwargs, elapsed, self.stale_timeout, inline=True
        )
        _touch_stale_kill_activity(self.agent, elapsed)

    def start_watchdogs(self) -> None:
        """Start the activity heartbeat and (for a finite budget) the stale timer."""
        self._hb.start()
        if math.isfinite(self.stale_timeout) and self.stale_timeout > 0:
            self._watchdog = threading.Timer(self.stale_timeout, self._on_stale)
            self._watchdog.name = "direct-api-stale-watchdog"
            self._watchdog.daemon = True
            self._watchdog.start()

    def stop_watchdogs(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        self.mark_done()
        self._hb_stop.set()
        self._hb.join(timeout=2.0)

    def _abort_client(self, client, reason: str, log_msg: str) -> None:
        try:
            self.agent._abort_request_openai_client(client, reason=reason)
        except Exception:
            logger.debug(log_msg, exc_info=True)

    def abort(self, reason: str) -> bool:
        """Abort the inline request from a watchdog/interrupt thread. Returns True
        when this call owned the stale transition (the timer reports/bumps once,
        never after an interrupt or a completed request). Aborts under the lock
        (same contract as _RequestClientRegistry): once released the finally may
        cache the client and the NEXT call check it out."""
        with self.lock:
            if self.done:
                return False
            if reason == "stale_call_kill":
                if self.cancelled:
                    return False
                newly_stale = not self.stale
                if newly_stale:
                    self.stale = True
                    # Bump BEFORE releasing: a fast retry's reset must not be
                    # overtaken by this older timer restoring the streak.
                    _bump_stale_streak(self.agent)
            else:
                # Interrupt wins the lock -> owns the outcome; a later timer
                # must not count it as staleness.
                self.cancelled = True
                newly_stale = False
            if self.client is not None:
                self._abort_client(self.client, reason, f"Inline request abort failed ({reason})")
            return newly_stale

    def make_client(self, reason: str, kind: str = "openai"):
        # Only OpenAI-wire requests reach direct_api_call; ``kind`` exists
        # for signature parity with the dispatch helper.
        client = self.agent._create_request_openai_client(reason=reason, api_kwargs=self.api_kwargs)
        with self.lock:
            self.client = client
            stale_before_dispatch = self.stale
            if stale_before_dispatch:
                # Timer fired during client construction: the abort found no
                # socket, so dispatching now would open one AFTER the only
                # watchdog fired. Fail here instead. (Residual ms-scale window
                # before httpx opens its socket is accepted.)
                self._abort_client(
                    client, "stale_call_kill", "Inline abort after late client registration failed"
                )
        if stale_before_dispatch:
            raise TimeoutError(
                f"Non-streaming API call timed out before request dispatch (threshold: {int(self.stale_timeout)}s)"
            )
        self.agent._active_request_abort = self.abort_hook
        return client

    def mark_done(self) -> None:
        with self.lock:
            self.done = True

    def pop_client(self):
        with self.lock:
            client, self.client = self.client, None
        return client


def direct_api_call(agent, api_kwargs: dict):
    """Run a non-streaming LLM call inline on the conversation thread (cron turns,
    delegated children — see ``should_use_direct_api_call``): no interrupt worker,
    so the nested-pool deadlock cannot occur. An activity heartbeat keeps
    ``last_activity_ts`` advancing (else the stall monitor interrupts a healthy
    wait at ~450s). A stale-call watchdog bounds the request (#80759): the timer
    aborts in-flight sockets via the registered hook, and a per-call ``timeout``
    equal to the stale budget is the backstop when the abort finds nothing (#85252).
    Both surface a retryable ``TimeoutError`` for the outer retry loop."""
    _check_stale_giveup(agent)
    agent._touch_activity("waiting for non-streaming API response")
    # Resolve the budget BEFORE the heartbeat starts: the resolver may raise
    # (fail-closed), and a leaked heartbeat thread would mask real stalls forever.
    call_start = time.time()
    stale_timeout = _resolve_direct_stale_timeout(agent, api_kwargs)
    # Never override an explicit per-call timeout; otherwise pin read=stale_timeout so a
    # no-op abort can't leave the read=None socket hanging until TCP dies (#85252).
    hard_timeout = _inline_nonstream_hard_timeout(stale_timeout)
    if hard_timeout is not None and "timeout" not in api_kwargs:
        api_kwargs = {**api_kwargs, "timeout": hard_timeout}
    request = _InlineRequest(agent, api_kwargs, stale_timeout, call_start)
    request.start_watchdogs()

    # Only a clean return reports the reuse reason; errors/interrupts really
    # close the client so the retry builds a fresh pool.
    succeeded = False
    try:
        response = _dispatch_nonstreaming_api_request(
            agent, api_kwargs, make_client=request.make_client
        )
    except Exception:
        if getattr(agent, "_interrupt_requested", False):
            raise InterruptedError("Agent interrupted during API call") from None
        with request.lock:
            was_stale = request.stale
        if was_stale:
            # Our own abort caused the transport error: raise a retryable
            # TimeoutError, never InterruptedError ("the user wants to stop").
            raise TimeoutError(
                f"Non-streaming API call timed out after {int(time.time() - call_start)}s with no response "
                f"(threshold: {int(stale_timeout)}s)"
            ) from None
        raise
    else:
        if getattr(agent, "_interrupt_requested", False):
            raise InterruptedError("Agent interrupted during API call")
        # Mark ``done`` under the lock so a timer firing between response
        # arrival and unwind is a no-op and cannot overwrite the reset below.
        # If a timer already won, the request still completed: return it (the
        # reset undoes the bump; the finally discards the poisoned client).
        request.mark_done()
        _reset_stale_streak(agent)
        succeeded = True
        return response
    finally:
        request.stop_watchdogs()
        if getattr(agent, "_active_request_abort", None) is request.abort_hook:
            agent._active_request_abort = None
        request_client = request.pop_client()
        if request_client is not None:
            agent._close_request_openai_client(
                request_client, reason="request_complete" if succeeded else "request_error_cleanup"
            )


def interruptible_api_call(agent: Any, api_kwargs: dict[str, Any]) -> Any:
    """Run the API call inline (see ``direct_api_call``)."""
    return direct_api_call(agent, api_kwargs)


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
