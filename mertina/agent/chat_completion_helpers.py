# Ported from hermes-agent agent/chat_completion_helpers.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""API-call helpers extracted from :class:`AIAgent`: non-streaming and streaming
request drivers, request kwargs builder, assistant-message materializer,
provider-fallback activator, max-iterations handler, per-turn resource cleanup.

Each function takes the parent ``AIAgent`` as ``agent``; AIAgent keeps thin
forwarders. Symbols tests patch on ``run_agent`` (``cleanup_vm`` /
``cleanup_browser``) are resolved through :func:`_ra` at call time.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import threading
import time
import uuid
from typing import Any

from mertina.agent.fast_mode import effective_request_overrides
from mertina.agent.message_content import flatten_message_text
from mertina.agent.message_metadata import append_message, stamp_message_timestamp
from mertina.agent.message_sanitization import (
    _sanitize_surrogates,
    sanitize_outbound_kwargs,
)

# Remote endpoints must never be fingerprinted: the probe waterfall is only valid for local/LM-Studio/Ollama
# boxes. Non-Ollama remotes (sglang, vLLM, OpenAI-compat) expose Ollama-compat endpoints that can
# misidentify and, without an api_key, return 401 on every leg (issue #89863).
from mertina.agent.sdk_transform_bypass import bypass_chat_sdk_request_transform
from mertina.agent.transports.chat_completions import (
    is_router_timeout_shim,
)
from mertina.agent.turn_context import substitute_api_content
from mertina.utils import base_url_host_matches, env_int

logger = logging.getLogger(__name__)


_OPENROUTER_PROVIDER_SORT_VALUES = {"throughput", "latency", "price"}


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


def _validated_openrouter_provider_sort(raw_sort: Any) -> str | None:
    """Return a normalized OpenRouter provider.sort value or None."""
    if not isinstance(raw_sort, str):
        return None
    sort_value = raw_sort.strip().lower()
    if not sort_value:
        return None
    if sort_value in _OPENROUTER_PROVIDER_SORT_VALUES:
        return sort_value
    logger.warning(
        "Ignoring invalid OpenRouter provider.sort value %r (allowed: %s)",
        raw_sort,
        ", ".join(sorted(_OPENROUTER_PROVIDER_SORT_VALUES)),
    )
    return None


def _provider_preferences_for_agent(agent) -> dict[str, Any]:
    """Build the validated provider-routing object shared by request paths.

    ``provider_routing.models.<id>`` overlays the flat constructor values for the CURRENT
    ``agent.model`` (so ``/model`` switches, fallbacks, and delegated children on another
    model each get their own pins without any surface re-plumbing the kwargs)."""
    flat = {
        "only": agent.providers_allowed,
        "ignore": agent.providers_ignored,
        "order": agent.providers_order,
        "sort": agent.provider_sort,
        "require_parameters": agent.provider_require_parameters,
        "data_collection": agent.provider_data_collection,
    }
    per_model = {}
    with contextlib.suppress(Exception):
        from mertina.cli.config import load_config_readonly
        from mertina.constants import resolve_per_model_provider_routing

        _pr = load_config_readonly().get("provider_routing")
        per_model = resolve_per_model_provider_routing(
            agent.model, (_pr or {}).get("models") if isinstance(_pr, dict) else None
        )
    merged = {**flat, **{k: v for k, v in per_model.items() if k in flat}}
    merged["sort"] = _validated_openrouter_provider_sort(merged["sort"])
    merged["require_parameters"] = True if merged["require_parameters"] else None
    return {key: value for key, value in merged.items() if value}


def _prompt_cache_scope_for_agent(agent) -> str | None:
    """Rotation-stable logical cache scope for *agent*, or None (transports then
    fall back to the physical session_id, so a failure never blocks the build)."""
    try:
        from mertina.agent.prompt_cache_scope import resolve_prompt_cache_scope_safe

        return resolve_prompt_cache_scope_safe(agent)
    except Exception:
        logger.debug("prompt-cache scope resolution failed", exc_info=True)
        return None


def _merge_nous_portal_messages_extra_body(agent, anthropic_kwargs: dict) -> dict:
    """Merge Portal ``tags`` / ``session_id`` onto an Anthropic Messages kwargs dict.
    The Nous profile is only consulted by the OpenAI-wire transport; ``session_id``
    only — never ``provider_preferences`` (an OpenAI-wire routing object)."""
    if getattr(agent, "provider", None) not in {"nous", "nous-portal", "nousresearch"}:
        return anthropic_kwargs
    try:
        from mertina.providers import get_provider_profile

        nous_profile = get_provider_profile("nous")
        if nous_profile is not None:
            anthropic_kwargs.setdefault("extra_body", {}).update(
                nous_profile.build_extra_body(session_id=getattr(agent, "session_id", None))
            )
    except Exception as exc:
        logger.debug("Nous Portal extra_body merge failed: %s", exc)
    return anthropic_kwargs


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
    _giveup = env_int("HERMES_STREAM_STALE_GIVEUP", 5)
    _streak = _stale_streak(agent)
    if _giveup > 0 and _streak >= _giveup:
        raise RuntimeError(
            "Provider has been unresponsive (no response received) for "
            f"{_streak} consecutive stale attempts — aborting this call to "
            "avoid an indefinite stall. Switch models or start a new session, then retry."
        )


def _bedrock_converse_call(api_kwargs: dict, *, stream: bool, on_stream_denied=None):
    """Pop the Hermes routing keys and call ``converse`` / ``converse_stream`` (boto3
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


def _consume_ephemeral_reasoning_off(agent) -> bool:
    """Consume the one-shot "answer without thinking" continuation flag.

    Set by the length-continuation path when a request returned reasoning but NO
    visible content (thinking ate the output cap); continuation turns never replay
    prior reasoning, so thinking ON would re-burn the budget. When True the caller
    overrides the wire reasoning_config with ``{"enabled": False, "effort": "none"}``
    for exactly the next call. Prompt-cache cost is bounded to ONE cold prefix write
    on config-sensitive providers (Anthropic, OpenAI) — far cheaper than four futile
    full-budget continuations.
    """
    consumed = bool(getattr(agent, "_ephemeral_reasoning_off", False))
    if consumed:
        agent._ephemeral_reasoning_off = False
    return consumed


def _reasoning_config_for_wire(agent):
    """``agent.reasoning_config`` with the one-shot reasoning-off override applied.

    Once the route has answered a disable with "reasoning is mandatory"
    (``agent._reasoning_disable_rejected``), every disable — configured or
    the one-shot continuation override — is dropped for the rest of the
    session: the request goes out without a reasoning config and the route
    applies its own default.
    """
    cfg = agent.reasoning_config
    ephemeral_off = _consume_ephemeral_reasoning_off(agent)
    if getattr(agent, "_reasoning_effort_rejected", False):
        # The route rejected the configured reasoning LEVEL itself (#100536: ``reasoning.effort:
        # max`` on an enabled config). Omit the reasoning fields for the rest of the session —
        # the route default — as the auxiliary ladder does; resending would 400 identically.
        agent._wire_reasoning_config = None
        return None
    if cfg is None:
        # Unset effort: the profile's default (custom/OpenAI-compatible: medium) rather than the
        # route's own, recorded below as what went out so a rejection of it lands in the branch above.
        from mertina.agent.reasoning_params import unset_reasoning_default

        cfg = unset_reasoning_default(agent)
    if getattr(agent, "_reasoning_disable_rejected", False):
        # The route rejects disables. Resend exactly what the session has
        # been sending — the user's own config — so the retry lands on the
        # same provider cache key as every prior request. Only a config that
        # is itself a disable changes, and that session has never sent
        # anything else, so nothing warm is lost: a route that said the
        # disable is *mandatory-on* gets the floor effort (closest to what
        # the user asked for); a relay that does not know the field gets
        # nothing (route default).
        if isinstance(cfg, dict) and (cfg.get("enabled") is False or cfg.get("effort") == "none"):
            if getattr(agent, "_reasoning_floor_required", False):
                from mertina.agent.auxiliary_reasoning_floor import REASONING_FLOOR_EFFORT

                floored = {**cfg, "enabled": True, "effort": REASONING_FLOOR_EFFORT}
                agent._wire_reasoning_config = floored
                return floored
            agent._wire_reasoning_config = None
            return None
        agent._wire_reasoning_config = cfg
        return cfg
    if ephemeral_off:
        cfg = {**(cfg or {}), "enabled": False, "effort": "none"}
    # What actually went out: the reasoning-rejection rung reads it to tell a rejected
    # disable (drop the disable) from a rejected level (drop the reasoning fields).
    agent._wire_reasoning_config = cfg
    return cfg


def _alias_tool_search_bridge_for_xai(agent, transport, tools_for_api):
    """xAI chat-completions reserves ``tool_search`` and 400s when the bridge declares
    it (#95003): rename the wire declaration; ``normalize_response`` maps calls back
    via the transport's ``_last_wire_aliases`` (reset here so a stale map can't
    reverse-map a name this request never aliased). Deep-copy first (#27907)."""
    if transport is not None and hasattr(transport, "_last_wire_aliases"):
        transport._last_wire_aliases = {}
    is_xai_chat = agent.provider in {"xai", "xai-oauth"} or agent._base_url_hostname == "api.x.ai"
    if not (is_xai_chat and tools_for_api):
        return tools_for_api
    try:
        import copy as _copy_xai

        from mertina.agent.transports.chat_completions import _rename_tool_search_bridge_for_xai

        has_bridge = any(
            (t.get("function") or {}).get("name") == "tool_search"
            for t in tools_for_api
            if isinstance(t, dict)
        )
        if has_bridge:
            tools_for_api = _copy_xai.deepcopy(tools_for_api)
            tools_for_api, alias_map = _rename_tool_search_bridge_for_xai(tools_for_api)
            if transport is not None:
                transport._last_wire_aliases = alias_map
    except Exception as exc:
        logger.warning(
            "%s⚠️ Failed to alias tool_search bridge for xAI: %s",
            getattr(agent, "log_prefix", ""),
            exc,
        )
    return tools_for_api


def _consume_ephemeral_max_output(agent):
    """Pop the one-shot ephemeral output cap; whichever path builds the request consumes it."""
    ephemeral_out = getattr(agent, "_ephemeral_max_output_tokens", None)
    if ephemeral_out is not None:
        agent._ephemeral_max_output_tokens = None
    return ephemeral_out


def _build_anthropic_kwargs(
    agent, api_messages, tools_for_api, reasoning_config, request_overrides
):
    ctx_len = getattr(agent, "context_compressor", None)
    ephemeral_out = _consume_ephemeral_max_output(agent)
    anthropic_kwargs = agent._get_transport().build_kwargs(
        model=agent.model,
        messages=agent._prepare_anthropic_messages_for_api(api_messages),
        tools=tools_for_api,
        max_tokens=ephemeral_out if ephemeral_out is not None else agent.max_tokens,
        reasoning_config=reasoning_config,
        is_oauth=agent._is_anthropic_oauth,
        preserve_dots=agent._anthropic_preserve_dots(),
        context_length=ctx_len.context_length if ctx_len else None,
        base_url=getattr(agent, "_anthropic_base_url", None),
        fast_mode=request_overrides.get("speed") == "fast",
        drop_context_1m_beta=bool(getattr(agent, "_oauth_1m_beta_disabled", False)),
    )
    # Portal reads ``tags`` / ``session_id`` on its Messages route too, but the profile hook
    # is only consulted by the OpenAI-wire transport — merge here to keep sticky routing.
    return _merge_nous_portal_messages_extra_body(agent, anthropic_kwargs)


def _build_bedrock_kwargs(agent, api_messages, tools_for_api):
    # Bedrock Converse — the adapter converts messages/tools and calls boto3 directly.
    return agent._get_transport().build_kwargs(
        model=agent.model,
        messages=api_messages,
        tools=tools_for_api,
        max_tokens=agent.max_tokens,
        region=getattr(agent, "_bedrock_region", None) or "us-east-1",
        guardrail_config=getattr(agent, "_bedrock_guardrail_config", None),
    )


def _build_codex_kwargs(
    agent, api_messages, tools_for_api, reasoning_config, request_overrides, cache_scope_id
):
    from mertina.agent.codex_responses_adapter import classify_responses_route
    from mertina.agent.native_compaction import native_compaction_context_management

    is_codex_backend, is_xai_responses, is_github_responses = classify_responses_route(agent)
    # Native server-side compaction (gpt-5.6 on direct OpenAI / ChatGPT Codex routes
    # only) — None on every other route/model, leaving the request unchanged.
    context_management = native_compaction_context_management(
        agent,
        is_codex_backend=is_codex_backend,
        is_xai_responses=is_xai_responses,
        is_github_responses=is_github_responses,
    )
    # xAI's /responses endpoint 400s on ``pattern``/``format`` schema keywords and on
    # ``enum`` values containing ``/`` — strip them (#27197). Deep-copy first: the
    # sanitizers mutate in place and tools_for_api aliases agent.tools (#27907).
    if is_xai_responses:
        try:
            import copy as _copy

            from mertina.tools.schema_sanitizer import strip_pattern_and_format, strip_slash_enum

            tools_for_api = _copy.deepcopy(tools_for_api)
            tools_for_api, _ = strip_pattern_and_format(tools_for_api)
            tools_for_api, _ = strip_slash_enum(tools_for_api)
        except Exception as exc:
            logger.warning(
                "%s⚠️ Failed to sanitize tool schemas for xAI: %s",
                getattr(agent, "log_prefix", ""),
                exc,
            )
    ephemeral_out = _consume_ephemeral_max_output(agent)
    return agent._get_transport().build_kwargs(
        model=agent.model,
        messages=agent._prepare_messages_for_non_vision_model(api_messages),
        tools=tools_for_api,
        reasoning_config=reasoning_config,
        session_id=getattr(agent, "session_id", None),
        cache_scope_id=cache_scope_id,
        base_url=agent.base_url,
        max_tokens=ephemeral_out if ephemeral_out is not None else agent.max_tokens,
        timeout=agent._resolved_api_call_timeout(),
        request_overrides=request_overrides,
        provider=getattr(agent, "provider", None),
        is_github_responses=is_github_responses,
        is_codex_backend=is_codex_backend,
        is_xai_responses=is_xai_responses,
        github_reasoning_extra=agent._github_models_reasoning_extra_body()
        if is_github_responses
        else None,
        replay_encrypted_reasoning=bool(getattr(agent, "_codex_reasoning_replay_enabled", True)),
        context_management=context_management,
        text_verbosity=getattr(agent, "text_verbosity", None),
    )


def _build_chat_completions_kwargs(
    agent, api_messages, tools_for_api, reasoning_config, request_overrides, cache_scope_id
):
    transport = agent._get_transport()
    tools_for_api = _alias_tool_search_bridge_for_xai(agent, transport, tools_for_api)

    _is_qwen = agent._is_qwen_portal()
    _is_or = agent._is_openrouter_url()
    _host = agent._base_url_lower
    _is_gh = base_url_host_matches(_host, "models.github.ai") or base_url_host_matches(
        _host, "githubcopilot.com"
    )
    _is_lmstudio = (agent.provider or "").strip().lower() == "lmstudio"

    # _fixed_temperature_for_model may return the OMIT_TEMPERATURE sentinel
    # (temperature omitted entirely), a numeric override, or None.
    _omit_temp, _fixed_temp = False, None
    with contextlib.suppress(Exception):
        from mertina.agent.auxiliary_client import OMIT_TEMPERATURE, _fixed_temperature_for_model

        _ft = _fixed_temperature_for_model(agent.model, agent.base_url)
        _omit_temp = _ft is OMIT_TEMPERATURE
        _fixed_temp = None if _omit_temp else _ft

    _prefs = _provider_preferences_for_agent(agent)

    _qwen_meta = (
        {"sessionId": agent.session_id or "hermes", "promptId": str(uuid.uuid4())}
        if _is_qwen
        else None
    )
    _profile = None
    with contextlib.suppress(Exception):
        from mertina.providers import get_provider_profile

        _profile = get_provider_profile(agent.provider)

    _ephemeral_out = _consume_ephemeral_max_output(agent)
    # Strip image parts for non-vision models on BOTH paths (registered
    # providers with profiles used to bypass it).
    _common = dict(
        model=agent.model,
        messages=agent._prepare_messages_for_non_vision_model(api_messages),
        tools=tools_for_api,
        base_url=agent.base_url,
        timeout=agent._resolved_api_call_timeout(),
        max_tokens=agent.max_tokens,
        ephemeral_max_output_tokens=_ephemeral_out,
        max_tokens_param_fn=agent._max_tokens_param,
        reasoning_config=reasoning_config,
        request_overrides=request_overrides,
        session_id=getattr(agent, "session_id", None),
        cache_scope_id=cache_scope_id,
        ollama_num_ctx=agent._ollama_num_ctx,
        provider_preferences=_prefs or None,
        openrouter_min_coding_score=agent.openrouter_min_coding_score,
        supports_reasoning=agent._supports_reasoning_extra_body(),
        qwen_session_metadata=_qwen_meta,
    )
    if _profile:
        # Profiles handle per-provider quirks via hooks fed the context above.
        return transport.build_kwargs(provider_profile=_profile, **_common)

    # Legacy flag path: only for a provider absent from the providers/ registry.
    return transport.build_kwargs(
        **_common,
        model_lower=(agent.model or "").lower(),
        is_openrouter=_is_or,
        is_nous=base_url_host_matches(_host, "nousresearch.com"),
        is_qwen_portal=_is_qwen,
        is_github_models=_is_gh,
        is_nvidia_nim=base_url_host_matches(_host, "integrate.api.nvidia.com"),
        is_kimi=any(
            base_url_host_matches(agent.base_url, h)
            for h in ("api.kimi.com", "moonshot.ai", "moonshot.cn")
        ),
        is_tokenhub=base_url_host_matches(_host, "tokenhub.tencentmaas.com"),
        is_lmstudio=_is_lmstudio,
        is_custom_provider=agent.provider == "custom",
        qwen_prepare_fn=agent._qwen_prepare_chat_messages if _is_qwen else None,
        qwen_prepare_inplace_fn=agent._qwen_prepare_chat_messages_inplace if _is_qwen else None,
        fixed_temperature=_fixed_temp,
        omit_temperature=_omit_temp,
        github_reasoning_extra=agent._github_models_reasoning_extra_body() if _is_gh else None,
        lmstudio_reasoning_options=agent._lmstudio_reasoning_options_cached()
        if _is_lmstudio
        else None,
        provider_name=agent.provider,
    )


def build_api_kwargs(agent, api_messages: list, tools_for_api: list | None = None) -> dict:
    """Build the keyword arguments dict for the active API mode.

    Wraps the per-api_mode builder so the conversation-affinity headers (OpenCode's
    ``x-opencode-session``, a custom provider's opt-in ``session_affinity_header``) ride on
    every request regardless of transport (chat_completions / codex_responses /
    anthropic_messages). No-op for every other provider.
    """
    from mertina.agent.opencode_affinity import merge_session_affinity_headers

    kwargs = _build_api_kwargs_for_mode(agent, api_messages, tools_for_api)
    return merge_session_affinity_headers(
        kwargs,
        getattr(agent, "provider", None),
        getattr(agent, "base_url", None),
        getattr(agent, "session_id", None),
    )


def _build_api_kwargs_for_mode(
    agent, api_messages: list, tools_for_api: list | None = None
) -> dict:
    # One-shot continuation override — consumed exactly once, on the FIRST
    # request this call builds (only one api_mode branch runs per invocation).
    reasoning_config = _reasoning_config_for_wire(agent)
    if tools_for_api is None:
        tools_for_api = agent.tools
    # The one place request_overrides are consumed: static /fast values are already pinned
    # in agent.request_overrides; auto/cold windows layer the fast override per request.
    request_overrides = effective_request_overrides(agent)
    if agent.api_mode == "anthropic_messages":
        return _build_anthropic_kwargs(
            agent, api_messages, tools_for_api, reasoning_config, request_overrides
        )
    if agent.api_mode == "bedrock_converse":
        return _build_bedrock_kwargs(agent, api_messages, tools_for_api)
    # Rotation-stable logical cache scope shared by every OpenAI-wire branch
    # (memoized on the agent); anthropic/bedrock above don't use it.
    cache_scope_id = _prompt_cache_scope_for_agent(agent)
    builder = (
        _build_codex_kwargs
        if agent.api_mode == "codex_responses"
        else _build_chat_completions_kwargs
    )
    return builder(
        agent, api_messages, tools_for_api, reasoning_config, request_overrides, cache_scope_id
    )


def _model_dump_safe(obj):
    """``model_dump(warnings=False)`` (avoids pydantic serializer UserWarnings on
    generic-union SDK models), falling back for shims that reject the kwarg."""
    try:
        return obj.model_dump(warnings=False)
    except TypeError:
        return obj.model_dump()


def _dump_if_model(value):
    return _model_dump_safe(value) if hasattr(value, "model_dump") else value


def _assistant_reasoning_text(agent, assistant_message) -> str | None:
    """Structured reasoning, else inline ``<think>`` blocks embedded in content."""
    reasoning_text = agent._extract_reasoning(assistant_message)
    if not reasoning_text:
        content = flatten_message_text(getattr(assistant_message, "content", None))
        think_blocks = re.findall(r"<think>(.*?)</think>", content, flags=re.DOTALL)
        if think_blocks:
            reasoning_text = "\n\n".join(b.strip() for b in think_blocks if b.strip()) or None
    if reasoning_text and agent.verbose_logging:
        logging.debug(f"Captured reasoning ({len(reasoning_text)} chars): {reasoning_text}")
    # When streaming is active the reasoning was already displayed during the
    # stream (structured deltas or <think> tag extraction); fire only for
    # non-streaming modes (gateway, batch, quiet). Anything not shown during
    # streaming is caught by the CLI post-response fallback.
    if (
        reasoning_text
        and agent.reasoning_callback
        and not agent.stream_delta_callback
        and not agent._stream_callback
    ):
        with contextlib.suppress(Exception):
            agent.reasoning_callback(reasoning_text)
    return _sanitize_surrogates(reasoning_text) if reasoning_text else reasoning_text


def _assistant_content_for_storage(agent, assistant_message):
    # Sanitize surrogates (Kimi/GLM via Ollama emit code points that crash json.dumps),
    # strip inline <think> tags at the storage boundary (they leaked to platforms and
    # polluted titles), then redact inlined credentials before the message enters
    # history / state.db / gateway delivery (no-op with HERMES_REDACT_SECRETS off).
    content = _sanitize_surrogates(
        flatten_message_text(getattr(assistant_message, "content", None))
    )
    if isinstance(content, str) and content:
        content = agent._strip_think_blocks(content).strip()
        if content:
            from mertina.agent.redact import redact_sensitive_text

            content = redact_sensitive_text(content)
    return content


def _assistant_tool_call_dict(agent, tool_call, index: int) -> dict:
    raw_id = getattr(tool_call, "id", None)
    call_id = getattr(tool_call, "call_id", None)
    if not isinstance(call_id, str) or not call_id.strip():
        call_id, _ = agent._split_responses_tool_id(raw_id)
    if not isinstance(call_id, str) or not call_id.strip():
        if isinstance(raw_id, str) and raw_id.strip():
            call_id = raw_id.strip()
        else:
            _fn = getattr(tool_call, "function", None)
            call_id = agent._deterministic_call_id(
                getattr(_fn, "name", "") if _fn else "",
                getattr(_fn, "arguments", "{}") if _fn else "{}",
                index,
            )
    call_id = call_id.strip()

    response_item_id = getattr(tool_call, "response_item_id", None)
    if not isinstance(response_item_id, str) or not response_item_id.strip():
        _, response_item_id = agent._split_responses_tool_id(raw_id)
    response_item_id = agent._derive_responses_function_call_id(
        call_id, response_item_id if isinstance(response_item_id, str) else None
    )
    # Arguments are deliberately NOT redacted: this dict is replayed to the model every
    # turn, so a ``***`` mask would break credential-dependent commands (#43083).
    tc_dict = {
        "id": call_id,
        "call_id": call_id,
        "response_item_id": response_item_id,
        "type": tool_call.type,
        "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments},
    }
    # Preserve extra_content (Gemini thought_signature) or Gemini 3 thinking
    # models 400 on the next request.
    # Tool-call arguments are intentionally NOT redacted here. This dict enters the in-memory conversation
    # history that is replayed to the model on every subsequent turn AND persisted to state.db, which is
    # itself replayed verbatim on session resume (get_messages_as_conversation). Masking a credential to
    # `***` here poisons that replay: the model reads back its own `PGPASSWORD='***' psql ...` call and
    # copies the placeholder into the next tool call, breaking every credential-dependent command on the
    # second turn (#43083). The masking also provided no real protection — the same secret still leaks
    # verbatim through tool OUTPUT (file contents, command output, diffs, the compaction block), none of
    # which this pass ever touched. Keeping secrets out of the replayable store is a separate
    # tokenization/vault concern, not something arg-redaction can deliver without breaking replay.
    # Storage-time redaction remains governed by the `security.redact_secrets` toggle. (#19798 introduced
    # this; #43083 removed it.) Preserve extra_content (e.g. Gemini thought_signature) so it is sent back on
    # subsequent API calls. Without this, Gemini 3 thinking models reject the request with a 400 error.
    extra = getattr(tool_call, "extra_content", None)
    if extra is not None:
        tc_dict["extra_content"] = _dump_if_model(extra)
    return tc_dict


def build_assistant_message(agent, assistant_message, finish_reason: str) -> dict:
    """Build a normalized assistant message dict (reasoning, reasoning_details,
    optional tool_calls) shared by the tool-call and final-response paths.
    Textless turns are NOT padded here: ``repair_empty_non_final_messages`` is the
    single owner — write-time padding broke codex commentary turns and cannot
    survive ``_rows_to_conversation``."""
    assistant_tool_calls = getattr(assistant_message, "tool_calls", None)
    reasoning_text = _assistant_reasoning_text(agent, assistant_message)
    msg = stamp_message_timestamp(
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
    elif assistant_tool_calls and agent._needs_thinking_reasoning_pad():
        # DeepSeek v4 / Kimi thinking modes 400 on a replayed tool-call message without
        # reasoning_content; pad with a single space (empty string is rejected too).
        # Without it, replaying the persisted message causes HTTP 400 ("The reasoning_content in the
        # thinking mode must be passed back to the API"). Include streamed reasoning text when captured;
        # otherwise pad with a single space — DeepSeek V4 Pro tightened validation and rejects empty string
        # ("The reasoning content in the thinking mode must be passed back to the API"). A space satisfies
        # non-empty checks everywhere without leaking fabricated reasoning. Refs #15250, #17400, #17341.
        msg["reasoning_content"] = reasoning_text or " "
    elif reasoning_text:
        # Streaming-only providers accumulate reasoning via deltas and never set
        # it on the message; replaying through a thinking model then 400s.
        # Promote ONLY when nothing set the field: SDK reasoning_content and the
        # tool-call pad win, and reasoning-less turns leave the field absent so
        # the replay-time leak guard and promotion tiers still apply.
        # Additive fallback (refs #16844, #16884). Streaming-only providers (glm, MiniMax, gpt-5.x via aigw,
        # Anthropic via openai-compat shims) accumulate reasoning through ``delta.reasoning_content`` chunks
        # but never land it on the message object as a top-level attribute, so neither branch above fires
        # and the chain-of-thought is stored only under the internal ``reasoning`` key. When the user later
        # replays that history through a DeepSeek-v4 / Kimi thinking model, the missing
        # ``reasoning_content`` causes HTTP 400 ("The reasoning_content in the thinking mode must be passed
        # back to the API."). Promote the already-sanitized streamed ``reasoning_text`` to
        # ``reasoning_content`` at write time, but ONLY when no prior branch already set it AND we actually
        # captured reasoning text. This preserves every existing behavior: - SDK-exposed
        # ``reasoning_content`` (OpenAI/Moonshot/DeepSeek SDK) still wins.
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

    # Provider-native carriers replayed verbatim on later turns:
    # anthropic_content_blocks keeps interleaved thinking + tool_use order
    # (reconstruction reorders signed blocks -> HTTP 400); codex_* items are
    # the encrypted reasoning / exact message items Responses prefix caching
    # needs.
    for attr in (
        "anthropic_content_blocks",
        "bedrock_content_blocks",
        "codex_reasoning_items",
        "codex_message_items",
    ):
        value = getattr(assistant_message, attr, None)
        if value:
            msg[attr] = value
            if attr == "codex_reasoning_items":
                from mertina.agent.codex_responses_adapter import (
                    has_replayable_native_compaction_checkpoint,
                )

                note_checkpoint = getattr(
                    agent.context_compressor, "note_native_compaction_checkpoint", None
                )
                if callable(note_checkpoint) and has_replayable_native_compaction_checkpoint(
                    agent, [msg]
                ):
                    note_checkpoint()

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


def _iteration_summary_api_messages(agent, messages: list) -> list:
    """Wire-ready messages for the summary call, mirroring the main loop's api_messages build
    (sidecar substitution, tool-call repair, thinking-only drop, underscore-key sweep).

    ``reasoning_details`` is kept: the anthropic_messages converter rebuilds signed thinking
    blocks from it, and the chat-completions transport already drops it on the wire for routes
    that do not replay it (``_chat_summary_attempt`` -> ``_build_api_kwargs``)."""
    needs_sanitize = agent._should_sanitize_tool_calls()
    sanitize_model = agent.model
    if needs_sanitize and agent.provider == "moa":
        # MoA: agent.model is the virtual preset; use the real aggregator so Gemini keeps thought_signature.
        agg_slot = getattr(getattr(agent, "client", None), "last_aggregator_slot", None)
        sanitize_model = (agg_slot or {}).get("model") or sanitize_model
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
        # api_content holds the exact bytes the main loop sent; substituting (not popping)
        # keeps the summary's prefix identical instead of re-prefilling the largest context.
        # Strict OpenAI-compatible gateways (Fireworks-backed OpenCode Go, Mistral, Moonshot/Kimi) reject
        # any message key outside the Chat Completions schema. The main loop drops these via
        # ChatCompletionsTransport.convert_messages(), but the summary path hand-builds messages and calls
        # chat.completions.create() directly, bypassing the transport — so mirror that sanitization here:
        # tool_name (SQLite FTS bookkeeping), the codex_* reasoning carriers, timestamp (preserved on
        # gateway user replay entries for the stale-confirmation expiry check — #47868 rejection class), and
        # every Hermes-internal underscore-prefixed scaffolding key.
        substitute_api_content(api_msg)
        if needs_sanitize:
            agent._sanitize_tool_calls_for_strict_api(api_msg, model=sanitize_model)
        api_messages.append(api_msg)

    effective_system = agent._cached_system_prompt or ""
    if agent.ephemeral_system_prompt:
        effective_system = (effective_system + "\n\n" + agent.ephemeral_system_prompt).strip()
    if effective_system:
        api_messages = [{"role": "system", "content": effective_system}] + api_messages
    for idx, pfm in enumerate(agent.prefill_messages or ()):
        api_messages.insert((1 if effective_system else 0) + idx, pfm.copy())

    # Compression/resume can orphan a tool result whose parent tool_call was summarized away.
    api_messages = agent._sanitize_api_messages(api_messages)
    # Same send-path vision eviction as the main loop (#89296).
    from mertina.agent.context_compressor import evict_stale_outbound_tool_images

    evict_stale_outbound_tool_images(api_messages)
    # Thinking-only assistant turns 400 on Anthropic-family providers; _thinking_prefill must
    # survive until here so the drop pass recognizes stubs after reasoning is stripped.
    api_messages = agent._drop_thinking_only_and_merge_users(api_messages)
    for api_msg in api_messages:  # underscore scaffolding: the transport's sweeper is bypassed here
        if isinstance(api_msg, dict):
            for internal_key in [k for k in api_msg if isinstance(k, str) and k.startswith("_")]:
                del api_msg[internal_key]
    return api_messages


def _managed_summary_call(agent, api_request_id: str, request, callback, *, retry_count: int):
    from mertina.agent import relay_llm

    return relay_llm.execute_current(
        request,
        callback,
        name=str(getattr(agent, "provider", "") or "provider"),
        model_name=str(getattr(agent, "model", "") or ""),
        metadata={
            "api_mode": str(getattr(agent, "api_mode", "") or "chat_completions"),
            "api_request_id": api_request_id,
            "call_role": "iteration_summary",
            "retry_count": retry_count,
        },
        defer_logical_completion=True,
    )


def _summary_text(agent, response, **normalize_kwargs) -> str:
    if is_router_timeout_shim(response):
        # Router failure in a 200 envelope (#68396): an empty summary takes the retry slot.
        logger.warning("Iteration summary returned a router timeout shim; retrying")
        return ""
    normalized = agent._get_transport().normalize_response(response, **normalize_kwargs)
    if normalized.tool_calls:
        # No summary path executes tool calls; log so a tool-only response that falls into the
        # empty-summary retry is diagnosable.
        logger.warning("Iteration summary emitted tool calls; discarding them")
    return (normalized.content or "").strip()


def _codex_summary_attempt(agent, api_messages: list, api_request_id: str):
    def _attempt(retry_count: int) -> str:
        codex_kwargs = agent._build_api_kwargs(api_messages)
        # The transport emits these three as one block (transports/codex.py build_kwargs);
        # strict Responses backends 400 on tool_choice/parallel_tool_calls without tools.
        codex_kwargs.pop("tools", None)
        codex_kwargs.pop("tool_choice", None)
        codex_kwargs.pop("parallel_tool_calls", None)
        return _summary_text(agent, agent._run_codex_stream(codex_kwargs))

    return _attempt


def _anthropic_summary_attempt(agent, api_messages: list, api_request_id: str):
    def _attempt(retry_count: int) -> str:
        ant_kw = agent._get_transport().build_kwargs(
            model=agent.model,
            messages=api_messages,
            tools=None,
            max_tokens=agent.max_tokens,
            reasoning_config=agent.reasoning_config,
            is_oauth=agent._is_anthropic_oauth,
            preserve_dots=agent._anthropic_preserve_dots(),
            base_url=getattr(agent, "_anthropic_base_url", None),
        )
        ant_kw = _merge_nous_portal_messages_extra_body(agent, ant_kw)
        response = _managed_summary_call(
            agent, api_request_id, ant_kw, agent._anthropic_messages_create, retry_count=retry_count
        )
        return _summary_text(agent, response, strip_tool_prefix=agent._is_anthropic_oauth)

    return _attempt


def _chat_summary_attempt(agent, api_messages: list, api_request_id: str):
    # Same kwargs builder as the main loop so the summary keeps the cached prefix (tools,
    # prompt_cache_key, xAI alias, Moonshot sanitization). Do not omit tools or force
    # tool_choice="none" here: SGLang renders the prompt with tools=None in that mode and the KV
    # prefix diverges. (cache_control breakpoint decoration is not re-applied on this path.)
    summary_kwargs = agent._build_api_kwargs(api_messages)
    # The summary now carries ``tools``; on cache-planned routes the main loop scrubbed a deep
    # copy, so ``agent.tools`` may still hold bytes the provider 400s on.
    sanitize_outbound_kwargs(agent, summary_kwargs)

    def _attempt(retry_count: int) -> str:
        summary_client = agent._ensure_primary_openai_client(
            reason="iteration_limit_summary_retry" if retry_count else "iteration_limit_summary"
        )
        response = _managed_summary_call(
            agent,
            api_request_id,
            summary_kwargs,
            lambda request: summary_client.chat.completions.create(
                **bypass_chat_sdk_request_transform(request, summary_client)
            ),
            retry_count=retry_count,
        )
        return _summary_text(agent, response)

    return _attempt


_SUMMARY_ATTEMPT_BUILDERS = {
    "codex_responses": _codex_summary_attempt,
    "anthropic_messages": _anthropic_summary_attempt,
}


def handle_max_iterations(agent, messages: list, api_call_count: int) -> str:
    """Request a summary when max iterations are reached. Returns the final response text."""
    warning = f"⚠️  Reached maximum iterations ({agent.max_iterations}). Requesting summary..."
    if getattr(agent, "suppress_status_output", False):
        # Strict machine-readable mode (-Q, oneshot): keep diagnostics off stdout. quiet_mode is
        # NOT the gate — the interactive CLI runs quiet_mode=True by default and must see this.
        # Strict machine-readable mode (hermes chat -Q, oneshot, background review): keep diagnostics out of
        # stdout so wrappers receive only the final assistant content (#93220 class).
        logger.warning(warning)
    else:
        agent._safe_print(warning, diagnostic=True)

    summary_api_request_id = f"iteration-summary:{uuid.uuid4()}"
    summary_call_outcome = "failed"

    # Shared constant so compaction recognizers can identify this runtime nudge by its stable
    # content after SessionDB projection strips metadata flags.
    from mertina.agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST

    append_message(messages, {"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST})

    try:
        api_messages = _iteration_summary_api_messages(agent, messages)
        build_attempt = _SUMMARY_ATTEMPT_BUILDERS.get(agent.api_mode, _chat_summary_attempt)
        attempt = build_attempt(agent, api_messages, summary_api_request_id)

        # One retry on an empty summary; a summary empty once its <think> block is stripped is NOT retried.
        final_response = _EMPTY_SUMMARY_RESPONSE
        for retry_count in (0, 1):
            text = attempt(retry_count)
            if not text:
                continue
            if "<think>" in text:
                text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
            if text:
                summary_call_outcome = "success"
                append_message(messages, {"role": "assistant", "content": text})
                final_response = text
            break

    except Exception as e:
        logger.warning("Failed to get summary response: %s", e)
        from mertina.agent.turn_failure_copy import site_copy

        final_response = site_copy("max_iterations_no_summary", limit=agent.max_iterations)
    finally:
        from mertina.agent import relay_llm

        relay_llm.complete_logical_call(summary_api_request_id, outcome=summary_call_outcome)

    return final_response
