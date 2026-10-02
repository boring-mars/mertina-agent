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
import logging
import os
import re
import threading
import time  # noqa: F401  # chat_completion_nonstream reads it as h.time
from collections.abc import Callable
from dataclasses import dataclass
from types import (
    SimpleNamespace,  # noqa: F401  # chat_completion_nonstream reads it as h.SimpleNamespace
)
from typing import Any

from mertina.agent.message_content import flatten_message_text
from mertina.agent.message_metadata import append_message, stamp_message_timestamp
from mertina.agent.message_sanitization import (
    _sanitize_surrogates,
)
from mertina.agent.model_metadata import is_local_endpoint
from mertina.agent.transports.base import ProviderTransport
from mertina.agent.transports.types import NormalizedResponse
from mertina.utils import env_float, env_int

logger = logging.getLogger(__name__)


def _context_thread_target(callback):
    """Bind a no-argument thread target to the caller's ContextVars."""
    context = contextvars.copy_context()
    return lambda: context.run(callback)


def _join_worker_for_relay_teardown(worker, *, label: str) -> None:
    """Bounded worker join before raising InterruptedError (#81521).

    Raising immediately lets turn teardown race a still-open Relay LLM scope and
    corrupt the LIFO stack (CLI EIO / redraw storm). Only joins when Relay managed
    execution is live — otherwise the join would just delay interrupt detection.
    """
    try:
        from mertina.agent import relay_runtime

        runtime = relay_runtime.get_runtime(create=False)
        if runtime is None or not runtime.managed_execution_enabled():
            return
    except Exception:
        return
    worker.join(timeout=2.0)
    if worker.is_alive():
        logger.warning(
            "%s worker still alive after interrupt abort (2.0s join "
            "timeout); Relay teardown will best-effort drain orphaned scopes (#81521).",
            label,
        )


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


def _is_openai_codex_backend(agent) -> bool:
    from mertina.agent.codex_responses_adapter import classify_responses_route

    return classify_responses_route(agent).is_codex_backend


def openai_codex_stale_timeout_floor(est_tokens: int) -> float:
    """Minimum wall-clock stale timeout for openai-codex by estimated context:
    subscription-backed Codex can spend minutes in admission/prefill on
    gateway-scale payloads, so the generic default would abort healthy calls.
    The floor engages above 10k estimated tokens."""
    for threshold, floor in ((100_000, 1200.0), (50_000, 900.0), (10_000, 600.0)):
        if est_tokens > threshold:
            return floor
    return 0.0


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


_INTERRUPTED_WAIT_STALE_SECONDS = 30.0


def _record_interrupted_provider_wait(agent, elapsed: float, *, response_started: bool) -> bool:
    """Count a user-aborted pre-response stall toward the stale breaker: past the
    wait-notice interval an interrupt is evidence of an unresponsive attempt.
    Mid-response and early interrupts stay neutral."""
    if response_started or elapsed < _INTERRUPTED_WAIT_STALE_SECONDS:
        return False
    _bump_stale_streak(agent)
    logger.warning(
        "Interrupted provider wait counted as stale after %.0fs with no output; "
        "consecutive stale attempts=%d.",
        elapsed,
        _stale_streak(agent),
    )
    return True


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


def _local_stream_stale_timeout_default() -> float:
    """Local-provider stale ceiling: ``agent.local_stream_stale_timeout`` (900s) or
    MERTINA_LOCAL_STREAM_STALE_TIMEOUT. Shared by the stream stale detector and the
    Responses first-event watchdog so both give a local server the same prefill grace."""
    local_default = 900.0
    with contextlib.suppress(Exception):
        from mertina.cli.config import load_config_readonly

        cfg = load_config_readonly()  # read-only consumer — no deepcopy
        agent_cfg = cfg.get("agent") if isinstance(cfg, dict) else None
        value = agent_cfg.get("local_stream_stale_timeout") if isinstance(agent_cfg, dict) else None
        if isinstance(value, (int, float)):
            local_default = float(value)
    return env_float("MERTINA_LOCAL_STREAM_STALE_TIMEOUT", local_default)


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
    """Run one non-streaming LLM request and return it.

    ``make_client(reason)`` builds the per-request client so callers can register it with their
    abort/close machinery. Interrupt/abort/close semantics stay in callers.
    """
    request_client = make_client("chat_completion_request")
    return request_client.chat.completions.create(**api_kwargs)


def should_use_direct_api_call(agent) -> bool:
    """Whether an OpenAI-wire request should skip the interrupt worker.

    Gateway cron turns (#62151) and delegated children (#60203) run inside nested
    thread pools that wedge before the socket opens when the request is pushed onto
    yet another daemon worker. Running inline drops the deepest layer; interrupts
    still work because the inline path registers ``agent._active_request_abort``,
    which ``interrupt()`` invokes cross-thread (#72227). Native/Codex/Bedrock/MoA
    keep their workers: their cancellation and client ownership differ.
    """
    if (
        getattr(agent, "api_mode", None) != "chat_completions"
        or getattr(agent, "provider", None) == "moa"
    ):
        return False
    if getattr(agent, "platform", None) == "cron":
        return True
    # Delegated child — via the execution ContextVar set by _run_single_child,
    # with the agent's platform stamp as a fallback for callers that bypass it.
    with contextlib.suppress(Exception):
        from mertina.agent.delegation_context import is_delegated_child_context

        if is_delegated_child_context():
            return True
    return getattr(agent, "platform", None) == "subagent"


class _InlineRequest:
    """Lifecycle state for one inline non-streaming request (#75301). Every transition
    happens under ``lock``: ``done`` stops a late interrupt aborting a client after unwind."""

    def __init__(self, agent):
        self.agent = agent
        self.client = None
        self.done = False
        self.lock = threading.Lock()
        self.abort_hook = self.abort  # single bound object: identity-checked on cleanup

    def _abort_client(self, client, reason: str, log_msg: str) -> None:
        try:
            self.agent._abort_request_openai_client(client, reason=reason)
        except Exception:
            logger.debug(log_msg, exc_info=True)

    def abort(self, reason: str) -> None:
        """Abort the inline request from an interrupt thread. Aborts under the lock: once
        released the finally may cache the client and the NEXT call check it out."""
        with self.lock:
            if self.done:
                return
            if self.client is not None:
                self._abort_client(self.client, reason, f"Inline request abort failed ({reason})")

    def make_client(self, reason: str):
        client = self.agent._create_request_openai_client(reason=reason)
        with self.lock:
            self.client = client
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
    """Run a non-streaming LLM call inline on the conversation thread: no interrupt worker.
    An interrupt aborts the in-flight sockets via the registered hook."""
    request = _InlineRequest(agent)

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
        raise
    else:
        if getattr(agent, "_interrupt_requested", False):
            raise InterruptedError("Agent interrupted during API call")
        request.mark_done()
        succeeded = True
        return response
    finally:
        if getattr(agent, "_active_request_abort", None) is request.abort_hook:
            agent._active_request_abort = None
        request_client = request.pop_client()
        if request_client is not None:
            agent._close_request_openai_client(
                request_client, reason="request_complete" if succeeded else "request_error_cleanup"
            )


class _RequestClientRegistry:
    """Per-request client / stream-handle registry shared by the request worker
    and the stranger threads (interrupt loop, stale detector) that may abort it.

    ``kind`` (``"openai"`` / ``"anthropic_messages"`` / ``"stream"``) routes
    :meth:`close_once` (#67142). ``"stream"`` registers a stream handle: under the
    MoA facade the singleton client has no per-request sockets, so interrupts
    must close the stream object itself (#57354).

    Thread-ownership rule (#29507): the owning worker pops + fully closes on its
    way out. A *stranger* thread only aborts the sockets — never ``client.close()``
    — avoiding the FD-recycling race where a just-closed TLS FD was reassigned to
    ``kanban.db`` and the live SSL BIO wrote into the SQLite header. The abort
    happens under the lock: once released the worker may cache the client and the
    NEXT call check it out. Stream handles are safe to close from any thread.
    """

    def __init__(self, agent):
        self.agent = agent
        self.client = None
        self.kind = "openai"
        self.owner_tid = None
        self.diag = None  # per-attempt stream diagnostics (streaming path)
        self.lock = threading.Lock()

    def set_client(self, client, *, kind: str = "openai"):
        with self.lock:
            self.client, self.kind, self.owner_tid = client, kind, threading.get_ident()
        return client

    @staticmethod
    def _stream_close_callable(stream):
        for owner in (stream, getattr(stream, "response", None)):
            close = getattr(owner, "close", None)
            if callable(close):
                return close
        return None

    def set_stream_handle(self, stream):
        return (
            stream
            if self._stream_close_callable(stream) is None
            else self.set_client(stream, kind="stream")
        )

    def _close_stream_handle(self, stream, reason: str) -> None:
        close = self._stream_close_callable(stream)
        if close is None:
            return
        try:
            close()
            logger.info("Streaming response handle closed (%s)", reason)
        except Exception as exc:
            logger.debug("Streaming response handle close failed (%s): %s", reason, exc)

    def close_once(self, reason: str) -> None:
        with self.lock:
            request_client, request_kind, owner_tid = self.client, self.kind, self.owner_tid
            stranger_thread = (
                request_kind != "stream"
                and request_client is not None
                and owner_tid is not None
                and owner_tid != threading.get_ident()
            )
            if stranger_thread:
                abort = (
                    self.agent._abort_request_anthropic_client
                    if request_kind == "anthropic_messages"
                    else self.agent._abort_request_openai_client
                )
                abort(request_client, reason=reason)
                return
            self.client = None
            self.owner_tid = None
        if request_client is None:
            return
        if request_kind == "stream":
            self._close_stream_handle(request_client, reason)
        elif request_kind == "anthropic_messages":
            self.agent._close_request_anthropic_client(request_client, reason=reason)
        else:
            self.agent._close_request_openai_client(request_client, reason=reason)


# Silence budget for high-or-above reasoning effort on a Codex request. GPT-5-family models at
# high effort think server-side for 100-170s before the first substantive SSE event even on a
# ~6KB prompt (#112909), while the token-sized tiers below hand such a prompt 12s/120s/90s; the
# watchdog killed healthy requests three times in a row and blamed the provider. Applies as a
# floor to the IMPLICIT defaults only -- explicit env/config values keep winning, and the stale
# timeout's run-budget cap is applied AFTER this floor (AIAgent._compute_non_stream_stale_timeout).
HIGH_EFFORT_SILENCE_FLOOR_SECONDS = 300.0


def _high_effort_silence_floor(agent) -> float:
    """``HIGH_EFFORT_SILENCE_FLOOR_SECONDS`` when the wire reasoning config is enabled at ``high`` or any
    stronger :data:`~agent.reasoning_effort.EFFORT_LADDER` level (xhigh/max/ultra), else 0."""
    from mertina.agent.reasoning_effort import EFFORT_LADDER

    cfg = getattr(agent, "reasoning_config", None)
    if not isinstance(cfg, dict) or cfg.get("enabled") is False:
        return 0.0
    effort = str(cfg.get("effort") or "").strip().lower()
    if effort not in EFFORT_LADDER or EFFORT_LADDER.index(effort) < EFFORT_LADDER.index("high"):
        return 0.0
    return HIGH_EFFORT_SILENCE_FLOOR_SECONDS


@dataclass
class _NonStreamWatchdogs:
    """Poll-loop thresholds for one non-streaming request."""

    stale_timeout: float
    codex: bool  # api_mode == codex_responses (codex watchdogs armed)
    est_tokens: int
    ttfb_enabled: bool
    ttfb_timeout: float
    idle_enabled: bool
    idle_timeout: float
    idle_requires_progress: bool


def _resolve_nonstream_watchdogs(agent, api_kwargs: dict) -> _NonStreamWatchdogs:
    """Stale-call timeout plus the Codex Responses stream watchdogs.

    The stale detector kills a hung provider early so the retry loop can rotate
    credentials / fall back. Codex adds two failure modes: accepting the connection
    but never emitting an event (no-event TTFB cutoff; a reconnect succeeds in ~2s)
    and stalling after substantive model progress begins (event-idle gap; any parsed SSE
    event remains transport activity). Only the implicit official OpenAI Codex policy
    for large contexts defers arming until progress; small requests, compatible backends,
    and explicit overrides retain the legacy first-event semantics. Tunables:
    MERTINA_CODEX_TTFB_TIMEOUT_SECONDS,
    MERTINA_CODEX_EVENT_STALE_TIMEOUT_SECONDS (0 disables each),
    MERTINA_CODEX_TTFB_DISABLE_ABOVE_TOKENS / MERTINA_CODEX_TTFB_STRICT,
    MERTINA_CODEX_TTFB_MAX_SECONDS (opt-in ceiling, default 0 = none), MERTINA_CODEX_HARD_TIMEOUT_SECONDS.
    """
    # The effort floor on the STALE timeout lives inside _compute_non_stream_stale_timeout so the
    # run-budget cap still bounds it; here the floor only raises the TTFB/idle implicit defaults.
    stale_timeout = agent._compute_non_stream_stale_timeout(api_kwargs)
    codex = agent.api_mode == "codex_responses"
    openai_codex_backend = _is_openai_codex_backend(agent)
    est_tokens = estimate_request_context_tokens(api_kwargs)
    effort_floor = _high_effort_silence_floor(agent) if codex else 0.0
    codex_floor = 0.0
    if codex and openai_codex_backend:
        # Raise the stale floor for large payloads so healthy gateway-scale
        # requests aren't aborted mid-prefill.
        codex_floor = openai_codex_stale_timeout_floor(est_tokens)
        if codex_floor:
            stale_timeout = max(stale_timeout, codex_floor)
        # Flat hard ceiling (#64507) for a request that emits SOME events then wedges.
        # Default sits ABOVE the max floor (1200s) — a backstop, never tighter. 0 disables.
        hard_timeout = env_float("MERTINA_CODEX_HARD_TIMEOUT_SECONDS", 1500.0)
        if hard_timeout > 0:
            stale_timeout = min(stale_timeout, hard_timeout)

    idle_default = max(
        effort_floor,
        next(
            (
                default
                for threshold, default in ((100_000, 180.0), (50_000, 120.0), (10_000, 60.0))
                if est_tokens > threshold
            ),
            12.0,
        ),
    )

    # No-event TTFB cutoff. Default 120s: the SDK's own read timeout is 600s,
    # and a tight 12s killed subscription-backed requests mid-prefill.
    ttfb_enabled = codex
    ttfb_explicit = env_float("MERTINA_CODEX_TTFB_TIMEOUT_SECONDS", -1.0) != -1.0
    ttfb_timeout = env_float("MERTINA_CODEX_TTFB_TIMEOUT_SECONDS", 120.0)
    if ttfb_timeout <= 0:
        ttfb_enabled = False
    elif openai_codex_backend:
        # Large requests legitimately spend tens of seconds in admission/prefill before the
        # first SSE event: scale the cutoff up to the idle default unless TTFB_STRICT is set.
        disable_above = env_float("MERTINA_CODEX_TTFB_DISABLE_ABOVE_TOKENS", 10_000.0)
        strict = os.environ.get("MERTINA_CODEX_TTFB_STRICT", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if (
            not strict
            and disable_above > 0
            and est_tokens >= disable_above
            and ttfb_timeout < idle_default
        ):
            logger.info(
                "Scaling openai-codex no-event TTFB watchdog from %.0fs to %.0fs "
                "for large request (context=~%s tokens >= %.0f). "
                "Set MERTINA_CODEX_TTFB_STRICT=1 to keep the smaller cutoff.",
                ttfb_timeout,
                idle_default,
                f"{est_tokens:,}",
                disable_above,
            )
            ttfb_timeout = idle_default
        # Opt-in ceiling (0 = off): a 120s default here silently undid the scale-up above (#91621).
        ttfb_cap = env_float("MERTINA_CODEX_TTFB_MAX_SECONDS", 0.0)
        if ttfb_cap > 0 and ttfb_timeout > ttfb_cap:
            logger.info(
                "Capping openai-codex no-event TTFB timeout from %.0fs to %.0fs "
                "(context=~%s tokens) per MERTINA_CODEX_TTFB_MAX_SECONDS.",
                ttfb_timeout,
                ttfb_cap,
                f"{est_tokens:,}",
            )
            ttfb_timeout = ttfb_cap
    elif (
        not ttfb_explicit
        and (base_url := getattr(agent, "base_url", None))
        and is_local_endpoint(base_url)
    ):
        # A local server prefills for minutes before its first event; the chat-completions
        # siblings already grant local endpoints the local stale ceiling, so the Responses
        # transport gets the same grace instead of the 120s hosted cutoff (#92302).
        local_ceiling = _local_stream_stale_timeout_default()
        if local_ceiling > ttfb_timeout:
            logger.info(
                "Local provider detected (%s) — no-event TTFB watchdog raised from %.0fs to %.0fs "
                "(agent.local_stream_stale_timeout); set MERTINA_CODEX_TTFB_TIMEOUT_SECONDS for an explicit cutoff.",
                base_url,
                ttfb_timeout,
                local_ceiling,
            )
            ttfb_timeout = local_ceiling
    if ttfb_enabled and not ttfb_explicit:
        # High-effort thinking precedes the first event; the floor outranks the cap.
        ttfb_timeout = max(ttfb_timeout, effort_floor)

    # An operator-set idle timeout keeps first-event semantics; only the implicit
    # default defers arming until model progress. Sentinel: env_float returns the
    # default for unset AND unparseable values, so both count as implicit.
    idle_explicit = env_float("MERTINA_CODEX_EVENT_STALE_TIMEOUT_SECONDS", -1.0) != -1.0
    idle_timeout = env_float("MERTINA_CODEX_EVENT_STALE_TIMEOUT_SECONDS", idle_default)
    return _NonStreamWatchdogs(
        stale_timeout=stale_timeout,
        codex=codex,
        est_tokens=est_tokens,
        ttfb_enabled=ttfb_enabled,
        ttfb_timeout=ttfb_timeout,
        idle_enabled=codex and idle_timeout > 0,
        idle_timeout=idle_timeout,
        idle_requires_progress=(
            codex and openai_codex_backend and codex_floor > 0 and not idle_explicit
        ),
    )


def _codex_silent_hang_hint(agent, api_kwargs: dict) -> str | None:
    hint_fn = getattr(agent, "_codex_silent_hang_hint", None)
    with contextlib.suppress(Exception):
        if callable(hint_fn):
            return hint_fn(model=api_kwargs.get("model"))
    return None


def interruptible_api_call(agent, api_kwargs: dict):
    """Run the API call on a worker thread so the caller can detect interrupts
    without waiting for the full HTTP round-trip. Each worker gets its own
    per-request client (interrupts close only that one); a stale-call detector
    kills the connection and raises so the main retry loop can back off / rotate
    credentials / fall back."""
    # Nested-pool contexts (cron, delegated children) wedge on a worker thread
    # (#62151): run inline. See should_use_direct_api_call.
    if should_use_direct_api_call(agent):
        return direct_api_call(agent, api_kwargs)
    _check_stale_giveup(agent)  # cross-turn stale breaker (#58962), non-streaming sibling
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
