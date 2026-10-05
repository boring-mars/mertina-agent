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
import time
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from mertina.agent.chat_completion_stream_monitor import StreamingWaitMonitor
from mertina.agent.error_classifier import (
    PROVIDER_STREAM_EMPTY_FRAME_ERROR_CODE,
    PROVIDER_STREAM_NON_JSON_ERROR_CODE,
    FailoverReason,
)
from mertina.agent.errors import EmptyStreamError
from mertina.agent.gemini_native_adapter import is_native_gemini_base_url
from mertina.agent.message_content import flatten_message_text
from mertina.agent.message_metadata import append_message, stamp_message_timestamp
from mertina.agent.message_sanitization import (
    _repair_tool_call_arguments,
    _sanitize_surrogates,
)
from mertina.agent.message_sanitization import (
    normalize_finish_reason as _normalize_finish_reason,
)
from mertina.agent.model_metadata import is_local_endpoint
from mertina.agent.reasoning_summaries import (
    append_streamed_reasoning_detail,
    separate_glued_reasoning_blocks,
)
from mertina.agent.sdk_transform_bypass import bypass_chat_sdk_request_transform
from mertina.agent.stream_single_writer import claim_stream_writer, stream_writer_is_current
from mertina.agent.transports.base import ProviderTransport
from mertina.agent.transports.chat_completions import (
    is_router_timeout_shim,
    router_timeout_shim_may_follow,
)
from mertina.agent.transports.types import NormalizedResponse
from mertina.cli.timeouts import get_provider_request_timeout, get_provider_stale_timeout
from mertina.constants import FINISH_REASON_LENGTH, PARTIAL_STREAM_STUB_ID
from mertina.utils import env_float, env_int

logger = logging.getLogger(__name__)


_PROVIDER_STREAM_ERROR_FINISH_REASONS = {"error", "error_finish"}
_PROVIDER_STREAM_SSE_FIELDS = {"event", "data", "id", "retry"}
_PROVIDER_STREAM_ERROR_TEXT_LIMIT = 4096


def _context_thread_target(callback: Callable[[], Any]) -> Callable[[], Any]:
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


class ProviderStreamError(Exception):
    """Provider encoded an API error as streaming content instead of an SDK error."""

    def __init__(self, *, status_code: int | None, body: dict, raw_text: str, headers: Any = None):
        self.status_code = status_code
        self.body = body
        self.raw_text = raw_text
        self.response = SimpleNamespace(headers=headers or {})
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        error_obj = self.body.get("error", {}) if isinstance(self.body, dict) else {}
        if not isinstance(error_obj, dict):
            error_obj = {}
        parts = ["Provider stream returned an error event"]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        if error_obj.get("code"):
            parts.append(str(error_obj["code"]))
        text = " - ".join(parts)
        if error_obj.get("message"):
            text += f": {error_obj['message']}"
        return text


def _status_code_from_value(value: Any) -> int | None:
    if isinstance(value, int) and 100 <= value < 600:
        return value
    if not isinstance(value, str):
        return None
    match = re.search(r"(?:HTTP_STATUS/)?\b([1-5]\d\d)\b", value, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _status_code_from_payload(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None

    candidates = [payload.get(k) for k in ("status_code", "status", "http_status")]
    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        candidates.extend(
            error_obj.get(k) for k in ("status_code", "status", "http_status", "code")
        )
    candidates.append(payload.get("code"))
    for candidate in candidates:
        status_code = _status_code_from_value(candidate)
        if status_code is not None:
            return status_code
    return None


def _json_object_from_text(text: str) -> dict | None:
    stripped = (text or "").strip()
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        if stripped.startswith("{"):
            decoded = json.loads(stripped)
            return decoded if isinstance(decoded, dict) else None
    return None


def _parse_provider_sse_events(text: str) -> list[dict]:
    """Parse provider text that looks like Server-Sent Events."""
    events: list[dict] = []
    current = {"event": None, "data": [], "comments": [], "fields": {}}

    def _flush_current():
        nonlocal current
        if any(current.values()):
            status_candidates = list(current["comments"]) + [
                current["fields"][key]
                for key in ("status", "status_code", "http_status")
                if key in current["fields"]
            ]
            events.append(
                {
                    "event": current["event"],
                    "data": "\n".join(current["data"]),
                    "comments": list(current["comments"]),
                    "fields": dict(current["fields"]),
                    "status_code": next(
                        (
                            s
                            for s in map(_status_code_from_value, status_candidates)
                            if s is not None
                        ),
                        None,
                    ),
                }
            )
        current = {"event": None, "data": [], "comments": [], "fields": {}}

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip("\r")
        if line == "":
            _flush_current()
            continue
        if line.startswith(":"):
            current["comments"].append(line[1:].strip())
            continue

        field, sep, value = line.partition(":")
        if not sep:
            current["fields"][field.strip().lower()] = ""
            continue
        field = field.strip().lower()
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            current["event"] = value.strip()
        elif field == "data":
            current["data"].append(value)
        else:
            current["fields"][field] = value

    _flush_current()
    return events


def _provider_error_body(payload: dict, status_code: int | None) -> dict:
    """Normalize common provider error payloads to OpenAI-style body.error."""
    if not isinstance(payload, dict):
        payload = {}
    elif isinstance(payload.get("error"), dict):
        return payload
    code = (
        payload.get("code")
        or payload.get("error_code")
        or payload.get("type")
        or (f"HTTP_{status_code}" if status_code else "provider_stream_error")
    )
    message = (
        payload.get("message")
        or payload.get("error_description")
        or payload.get("error")
        or "Provider stream returned an error event."
    )
    normalized_error = {"message": str(message)}
    if code:
        normalized_error["code"] = str(code)
    for key in ("request_id", "param", "type"):
        if payload.get(key):
            normalized_error[key] = payload[key]
    return {"error": normalized_error}


def _provider_stream_error_from_json_decode_error(
    error: json.JSONDecodeError, *, response: Any = None
) -> ProviderStreamError:
    """Preserve plain-text SSE data rejected inside the OpenAI SDK: on a non-JSON
    ``event: error`` the SDK raises from ``sse.json()`` before yielding a chunk,
    but ``JSONDecodeError.doc`` still carries the provider's original message.

    An EMPTY ``doc`` is the other case: the frame carried no payload at all
    (``data:`` / ``event: ping`` / ``id:`` alone — legal SSE keepalives and no-ops),
    which the SDK's ``json.loads`` rejects the same way. A gateway that is degrading
    answers EVERY streaming request with such frames, so this is not the provider's
    malformed payload and must not be reported as one: it gets its own code and
    the stream helper recovers by retrying without streaming."""
    from mertina.agent.redact import redact_sensitive_text

    raw_text = str(getattr(error, "doc", "") or "").strip()
    headers = getattr(response, "headers", None) if response is not None else None
    if not raw_text:
        return ProviderStreamError(
            status_code=None,
            body=_provider_error_body(
                {
                    "code": PROVIDER_STREAM_EMPTY_FRAME_ERROR_CODE,
                    "message": "Provider stream returned an empty SSE data frame (keepalive with no payload).",
                },
                None,
            ),
            raw_text="",
            headers=headers,
        )
    safe_text = redact_sensitive_text(_sanitize_surrogates(raw_text), force=True)
    safe_text = safe_text[:_PROVIDER_STREAM_ERROR_TEXT_LIMIT]
    return ProviderStreamError(
        status_code=None,
        body=_provider_error_body(
            {
                "code": PROVIDER_STREAM_NON_JSON_ERROR_CODE,
                "message": safe_text or "Provider stream returned non-JSON SSE data.",
            },
            None,
        ),
        raw_text=safe_text,
        headers=headers,
    )


def _is_provider_stream_empty_frame_error(exc: BaseException) -> bool:
    """True for the translated contentless-SSE-frame error. Re-streaming cannot help
    (a degraded gateway answers every stream that way), so the caller must change channel."""
    body = getattr(exc, "body", None)
    error_obj = body.get("error") if isinstance(body, dict) else None
    return (
        isinstance(error_obj, dict)
        and error_obj.get("code") == PROVIDER_STREAM_EMPTY_FRAME_ERROR_CODE
    )


def _iter_provider_stream_chunks(stream, *, response: Any = None):
    """Yield SDK chunks while translating SDK-level SSE decode failures."""
    try:
        yield from stream
    except json.JSONDecodeError as error:
        stream_response = response() if callable(response) else response
        if stream_response is None:
            stream_response = getattr(stream, "response", None)
        raise _provider_stream_error_from_json_decode_error(
            error, response=stream_response
        ) from error


def _payload_has_error_shape(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("error"), (dict, str)):
        return True
    return bool(payload.get("message")) and bool(
        payload.get("code")
        or payload.get("error_code")
        or _status_code_from_payload(payload) is not None
    )


def _provider_stream_text_may_be_sse(text: str) -> bool:
    """Return True while pending text still looks like an SSE control block."""
    stripped = (text or "").lstrip()
    if not stripped:
        return False

    lines = stripped.splitlines()
    trailing_newline = stripped.endswith(("\n", "\r"))
    saw_sse_field = False

    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip("\r")
        if line == "":
            continue
        if line.startswith(":"):
            saw_sse_field = True
            continue

        field, sep, _value = line.partition(":")
        field_name = field.strip().lower()
        if sep and field_name in _PROVIDER_STREAM_SSE_FIELDS:
            saw_sse_field = True
            continue

        is_last_incomplete = index == len(lines) - 1 and not trailing_newline
        if is_last_incomplete and any(
            sse_field.startswith(field_name) for sse_field in _PROVIDER_STREAM_SSE_FIELDS
        ):
            return True
        return False

    return saw_sse_field


def _provider_stream_error_from_text(
    text: str, finish_reason: str | None, *, response: Any = None
) -> ProviderStreamError | None:
    """Convert provider-streamed error text into an exception for retry logic."""
    if not text:
        return None

    if str(finish_reason or "").lower() not in _PROVIDER_STREAM_ERROR_FINISH_REASONS:
        return None

    headers = getattr(response, "headers", None) if response is not None else None

    def _error(payload: dict, status_code: int | None) -> ProviderStreamError:
        return ProviderStreamError(
            status_code=status_code,
            body=_provider_error_body(payload, status_code),
            raw_text=text,
            headers=headers,
        )

    for event in _parse_provider_sse_events(text):
        is_error_event = str(event.get("event") or "").strip().lower() == "error"
        payload = _json_object_from_text(event.get("data") or "") or {}
        status_code = event.get("status_code") or _status_code_from_payload(payload)
        # The finish_reason is an error here, so an error event always qualifies;
        # a non-error event needs an error-shaped payload or an HTTP error code.
        if (
            (status_code is not None and status_code >= 400)
            or is_error_event
            or _payload_has_error_shape(payload)
        ):
            return _error(payload, status_code)

    payload = _json_object_from_text(text)
    if payload is not None:
        return _error(payload, _status_code_from_payload(payload))

    if text.strip():
        return _error({}, None)
    return None


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


def _estimate_chunk_bytes(chunk: Any) -> int:
    """Cheap per-chunk size estimate for the stream diagnostic counters: delta
    string lengths plus a framing floor (~3x cheaper than ``len(repr(chunk))``
    in the agent's hottest loop). Unknown shapes just keep the floor."""
    size = 40  # SSE/JSON framing floor per chunk

    def _add(obj, *attrs):
        nonlocal size
        for attr in attrs:
            v = getattr(obj, attr, None)
            if isinstance(v, str):
                size += len(v)

    with contextlib.suppress(Exception):
        choices = getattr(chunk, "choices", None)
        if choices:
            delta = getattr(choices[0], "delta", None)
            if delta is not None:
                _add(delta, "content", "reasoning_content", "reasoning")
                for tc in getattr(delta, "tool_calls", None) or ():
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        _add(fn, "arguments", "name")
        else:
            _add(getattr(chunk, "delta", None), "text", "partial_json")
    return size


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


def _configured_stale_base(agent) -> float:
    """Per-provider ``stale_timeout_seconds`` config, else MERTINA_STREAM_STALE_TIMEOUT (180s)."""
    cfg = get_provider_stale_timeout(agent.provider, agent.model)
    return cfg if cfg is not None else env_float("MERTINA_STREAM_STALE_TIMEOUT", 180.0)


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


def _scale_stale_timeout_for_context(base: float, est_tokens: int) -> float:
    """Large contexts: slow models think for minutes before the first token;
    scale the threshold or the detector kills healthy streams."""
    if est_tokens > 100_000:
        return max(base, 300.0)
    if est_tokens > 50_000:
        return max(base, 240.0)
    return base


def _cloud_stale_timeout(base: float, api_kwargs: dict) -> float:
    """Cloud stale-stream patience: ``base`` scaled for context size, then floored for
    known reasoning models. ``model`` (OpenAI/Anthropic) wins over ``modelId`` (Bedrock);
    Bedrock's dotted, region-prefixed profile id can't match the floor's slug regex
    directly, so it is normalized as a fallback."""
    from mertina.agent.reasoning_timeouts import get_reasoning_stale_timeout_floor

    timeout = _scale_stale_timeout_for_context(base, estimate_request_context_tokens(api_kwargs))
    floor = get_reasoning_stale_timeout_floor(
        api_kwargs.get("model") or api_kwargs.get("modelId") or ""
    )
    if floor is None and api_kwargs.get("modelId"):
        floor = _bedrock_reasoning_stale_floor(api_kwargs["modelId"])
    return timeout if floor is None else max(timeout, floor)


def _derive_stream_stale_timeout(agent, api_kwargs: dict) -> float:
    """Stale-stream patience for a provider that is never a local endpoint (Bedrock):
    the OpenAI/Anthropic stale detector's budget minus its local branch."""
    return _cloud_stale_timeout_for(agent, api_kwargs)


def _cloud_stale_timeout_for(agent, api_kwargs: dict) -> float:
    """An explicit ``providers.<id>.stale_timeout_seconds`` is the operator's deadline and
    wins over every implicit floor — the context-size tier as well as the reasoning-model
    floor — so it can SHORTEN patience for a hung stream (#115024). Only the 180s default
    is scaled and floored."""
    explicit = get_provider_stale_timeout(agent.provider, agent.model)
    if explicit is not None:
        return explicit
    return _cloud_stale_timeout(env_float("MERTINA_STREAM_STALE_TIMEOUT", 180.0), api_kwargs)


def _bedrock_reasoning_stale_floor(model_id: object) -> float | None:
    """Map a Bedrock inference-profile id to its reasoning stale-timeout floor.

    ``us.anthropic.claude-opus-4-6-v1:0`` -> strip the region prefix, then try the
    segment after the provider namespace (``claude-opus-4-6-v1:0``) and the id with
    the provider dot dashed (``deepseek-r1-v1:0``). The floor table mixes dashed
    and dotted versions while Bedrock always dashes, so each candidate is also
    tried with digit-dash-digit <-> digit-dot-digit swapped (version separators
    only). First non-None wins; None for unknown models.
    """
    from mertina.agent.reasoning_timeouts import get_reasoning_stale_timeout_floor

    if not model_id or not isinstance(model_id, str):
        return None
    name = model_id.strip().lower()
    for prefix in (
        "global.",
        "us.",
        "eu.",
        "apac.",
        "ap.",
        "au.",
        "jp.",
        "ca.",
        "sa.",
        "me.",
        "af.",
    ):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    base_candidates = [name]
    if "." in name:
        base_candidates.append(name.rsplit(".", 1)[1])  # claude-opus-4-6-v1:0
        base_candidates.append(name.replace(".", "-", 1))  # deepseek-r1-v1:0
    candidates = dict.fromkeys(
        form
        for cand in base_candidates
        for form in (
            cand,
            re.sub(r"(?<=\d)-(?=\d)", ".", cand),
            re.sub(r"(?<=\d)\.(?=\d)", "-", cand),
        )
    )
    return next(
        (f for f in map(get_reasoning_stale_timeout_floor, candidates) if f is not None), None
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


def _dispatch_nonstreaming_api_request(
    agent: Any, api_kwargs: dict[str, Any], *, make_client: Callable[[str], Any]
) -> Any:
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
    role,
    full_content,
    full_reasoning,
    model_name,
    usage_obj,
    *,
    dropped_tool_names=None,
    overflow_terminal=False,
):
    """Stub for an SSE stream that ended without ``finish_reason`` after
    delivering content. Tagged ``PARTIAL_STREAM_STUB_ID`` + ``FINISH_REASON_LENGTH``
    so the loop enters its continuation/retry path instead of accepting
    truncated output as a complete turn (#32086).

    ``overflow_terminal`` (``full_content=None``): the stream died on a
    context-overflow error. Seeding the recovered text as a continuation stub
    would grow every later request into the same overflow (#106260); the loop
    treats the marker as terminal and ends the turn via the recovery contract.
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
        _dropped_tool_names=dropped_tool_names or None,
        _overflow_terminal=overflow_terminal,
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


def _relay_stream_identity(agent, name_default: str) -> dict:
    """``session_id``/``name``/``model_name`` kwargs for ``relay_llm.stream``."""
    return {
        "session_id": str(getattr(agent, "session_id", "") or ""),
        "name": str(getattr(agent, "provider", "") or name_default),
        "model_name": str(getattr(agent, "model", "") or ""),
    }


def _relay_stream_metadata(agent, api_mode: str) -> dict:
    call_role = (
        "delegated"
        if getattr(agent, "is_subagent", False)
        else "fallback"
        if int(getattr(agent, "_fallback_index", 0) or 0) > 0
        else "primary"
    )
    return {
        "api_mode": api_mode,
        "api_request_id": getattr(agent, "_current_api_request_id", None),
        "call_role": call_role,
    }


def _stream_final_text(response) -> str:
    with contextlib.suppress(Exception):
        choices = getattr(response, "choices", None)
        first_choice = choices[0] if isinstance(choices, (list, tuple)) and choices else None
        content = getattr(getattr(first_choice, "message", None), "content", None)
        if isinstance(content, str):
            return content
    with contextlib.suppress(Exception):
        content = getattr(response, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                t for t in (getattr(part, "text", None) for part in content) if isinstance(t, str)
            )
    return ""


def _with_stream_emitters(agent, run):
    """Bracket ``run()`` with the agent's ``_emit_stream_start`` / ``_emit_stream_end``
    hooks when present (end carries the final text on success, the error string on
    failure) and re-raise."""
    start = getattr(agent, "_emit_stream_start", None)
    if start is not None:
        start()
    try:
        response = run()
    except Exception as exc:
        end = getattr(agent, "_emit_stream_end", None)
        if end is not None:
            end(final_text="", finished=False, error=str(exc))
        raise
    end = getattr(agent, "_emit_stream_end", None)
    if end is not None:
        end(final_text=_stream_final_text(response), finished=True, error=None)
    return response


def _stream_codex_passthrough(agent, api_kwargs: dict, on_first_delta):
    """Codex streams internally via _run_codex_stream (reached through
    _interruptible_api_call); park ``on_first_delta`` on the agent so it can pick
    it up, and bracket the call with the stream start/end emitters."""
    agent._codex_on_first_delta = on_first_delta
    try:
        return _with_stream_emitters(agent, lambda: agent._interruptible_api_call(api_kwargs))
    finally:
        agent._codex_on_first_delta = None


class _BedrockStream:
    """Bedrock Converse streaming: boto3 ``converse_stream()`` on a worker thread
    with real-time delta callbacks, polled by an interrupt / stale-event watchdog
    (same UX as the Anthropic and chat_completions streams)."""

    def __init__(self, agent, api_kwargs: dict, on_first_delta):
        self.agent = agent
        self.api_kwargs = api_kwargs
        self.on_first_delta = on_first_delta
        self.result = {"response": None, "error": None}
        self.first_delta_fired = False
        self.response_started = False
        # Liveness for the boto3 worker: ``for event in event_stream`` has NO read timeout,
        # so on_event stamps every event and the poll loop trips a watchdog on a long gap.
        self.started_at = time.time()
        self.last_event = self.started_at
        # Read (not popped): the worker's own pop inside _open_stream must
        # still resolve the same region.
        self.region = api_kwargs.get("__bedrock_region__", "us-east-1")
        # Same patience budget as the OpenAI/Anthropic stale detector.
        self.stale_timeout = _derive_stream_stale_timeout(agent, api_kwargs)

    def _model(self) -> str:
        return self.api_kwargs.get("modelId", "unknown")

    def _fire_first(self):
        self.response_started = True
        if not self.first_delta_fired and self.on_first_delta:
            self.first_delta_fired = True
            with contextlib.suppress(Exception):
                self.on_first_delta()

    def _after_first(self, fire):
        """Wrap a delta callback so the first delivered event also fires ``on_first_delta``."""

        def _on(value):
            self._fire_first()
            fire(value)

        return _on

    def _open_stream(self, next_api_kwargs: dict[str, Any]):
        return _bedrock_converse_call(
            dict(next_api_kwargs), stream=True, on_stream_denied=self._fall_back_to_converse
        )

    def _fall_back_to_converse(self, client, final_kwargs: dict, exc: Exception):
        # InvokeModel-only IAM policies cannot stream; fall back inside the same Relay
        # attempt (one lifecycle boundary).
        from mertina.agent.bedrock_adapter import normalize_converse_response

        self.agent._disable_streaming = True
        self.agent._safe_print(
            "\n⚠  AWS IAM denied bedrock:InvokeModelWithResponseStream — "
            "falling back to non-streaming InvokeModel.\n"
            "   Grant that action to restore streaming output.\n",
            diagnostic=True,
        )
        logger.info(
            "bedrock: converse_stream denied by IAM (%s) — "
            "using non-streaming converse() for this session.",
            type(exc).__name__,
        )
        return normalize_converse_response(client.converse(**final_kwargs))

    def _worker(self):
        agent = self.agent
        stream = None
        try:
            from mertina.agent import relay_llm
            from mertina.agent.bedrock_adapter import stream_converse_with_callbacks

            intercepted_events = []
            writer_token = {"value": None}

            def _stream_created(_stream: Any) -> None:
                writer_token["value"] = claim_stream_writer(agent)

            def _accept_event(_event: Any) -> bool:
                token = writer_token["value"]
                return token is None or stream_writer_is_current(agent, token)

            def _stamp_event() -> None:
                self.last_event = time.time()

            try:
                from mertina.agent.plugin_stream_hooks import has_reasoning_stream_observer_hooks

                plugin_reasoning_observer = has_reasoning_stream_observer_hooks()
            except Exception:
                logger.debug("plugin reasoning stream observer check failed", exc_info=True)
                plugin_reasoning_observer = False

            stream = relay_llm.stream(
                dict(self.api_kwargs),
                self._open_stream,
                **_relay_stream_identity(agent, "bedrock"),
                finalizer=lambda: stream_converse_with_callbacks(
                    {"stream": list(intercepted_events)}
                ),
                on_stream_created=_stream_created,
                on_chunk=intercepted_events.append,
                chunk_adapter=lambda chunk: chunk,
                accept_chunk=_accept_event,
                completed_response_predicate=lambda response: bool(
                    getattr(response, "choices", None)
                ),
                metadata=_relay_stream_metadata(agent, "custom"),
                defer_logical_completion=True,
            )
            wants_reasoning = (
                agent.reasoning_callback or agent.stream_delta_callback or plugin_reasoning_observer
            )
            streamed_response = stream_converse_with_callbacks(
                {"stream": stream},
                on_text_delta=self._after_first(agent._fire_stream_delta)
                if agent._has_stream_consumers()
                else None,
                on_tool_start=self._after_first(agent._fire_tool_gen_started),
                on_reasoning_delta=self._after_first(agent._fire_reasoning_delta)
                if wants_reasoning
                else None,
                on_interrupt_check=lambda: agent._interrupt_requested,
                on_event=_stamp_event,
            )
            self.result["response"] = stream.final_response or streamed_response
        except Exception as e:
            self.result["error"] = e
        finally:
            if stream is not None:
                stream.close()

    def _raise_if_interrupted(self, message: str, worker=None) -> None:
        if not self.agent._interrupt_requested:
            return
        _record_interrupted_provider_wait(
            self.agent, time.time() - self.started_at, response_started=self.response_started
        )
        if worker is not None:
            # Let the worker unwind Relay scopes before raising (#81521).
            _join_worker_for_relay_teardown(worker, label="Bedrock streaming")
        raise InterruptedError(message)

    def _on_stale(self, stale_elapsed: float) -> None:
        """No event past the stale timeout = wedged stream (the worker would
        block in the event loop forever)."""
        agent = self.agent
        logger.warning(
            "Bedrock stream stale for %.0fs (threshold %.0fs) — no events "
            "received. region=%s model=%s. Aborting call.",
            stale_elapsed,
            self.stale_timeout,
            self.region,
            self._model(),
        )
        agent._buffer_diagnostic_status(
            f"⚠️ No events from Bedrock for {int(stale_elapsed)}s (model: {self._model()}). Aborting..."
        )
        _bump_stale_streak(agent)
        # Evict the region's cached client so the NEXT call gets a fresh pool.
        # This does NOT abort the in-flight botocore EventStream (no external
        # cancellation exists); the daemon worker keeps reading until its
        # socket errors, so THIS call ends via the TimeoutError below.
        try:
            from mertina.agent.bedrock_adapter import invalidate_runtime_client

            invalidate_runtime_client(self.region)
        except Exception as _inval_exc:
            logger.debug("bedrock: stale client eviction failed: %s", _inval_exc)
        self.last_event = time.time()
        # Raises RuntimeError past MERTINA_STREAM_STALE_GIVEUP; otherwise end
        # THIS call with a TimeoutError and let the streak carry forward.
        _check_stale_giveup(agent)
        self.result["error"] = TimeoutError(
            f"Bedrock stream produced no events for {int(stale_elapsed)}s (threshold {int(self.stale_timeout)}s) "
            f"— aborting stalled stream so the retry/fallback path can recover."
        )

    def _poll(self):
        t = threading.Thread(target=_context_thread_target(self._worker), daemon=True)
        t.start()
        while t.is_alive():
            t.join(timeout=0.3)
            self._raise_if_interrupted("Agent interrupted during Bedrock API call", worker=t)
            stale_elapsed = time.time() - self.last_event
            if stale_elapsed > self.stale_timeout:
                self._on_stale(stale_elapsed)
                break
        # The Bedrock callback returns a PARTIAL response on interrupt without raising
        # (on_interrupt_check), so the in-loop raise may never fire. Re-check (#59999 area).
        self._raise_if_interrupted("Agent interrupted during Bedrock API call (post-worker)")
        if self.result["error"] is not None:
            raise self.result["error"]
        # Success clears the cross-turn breaker (#58962).
        if self.result["response"] is not None:
            _reset_stale_streak(self.agent)
        return self.result["response"]

    def run(self):
        # Cross-turn stale-stream circuit breaker (#58962), as on the OpenAI/
        # Anthropic path.
        _check_stale_giveup(self.agent)
        return _with_stream_emitters(self.agent, self._poll)


class _ToolCallAccumulator:
    """Assemble streamed tool-call deltas into complete ``tool_calls`` entries
    (``acc``: slot index -> entry dict). Ollama-compatible endpoints reuse index 0
    for every call in a parallel batch, distinguishing them only by id, so a new
    id at an already-seen raw index is redirected to a fresh slot."""

    def __init__(self):
        self.acc: dict = {}
        self._notified: set = set()
        self._last_id_at_idx: dict = {}  # raw_index -> last seen non-empty id
        self._active_slot_by_idx: dict = {}  # raw_index -> current slot in acc
        # Argument deltas are collected per slot and joined once in ``materialize`` —
        # ``+=`` per chunk rebuilds the whole string every delta (quadratic on big args).
        self._argument_parts: dict[int, list[str]] = {}

    def materialize(self) -> dict:
        """Join buffered argument deltas into each entry's ``arguments``; idempotent. Returns ``acc``."""
        for idx, parts in self._argument_parts.items():
            self.acc[idx]["function"]["arguments"] = "".join(parts)
        return self.acc

    def feed(self, tc_delta) -> str | None:
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
            return name
        return None


class _StreamingCall(StreamingWaitMonitor):
    """One streaming request on the chat_completions / anthropic_messages wire.
    State shared between the request worker and the poll-loop monitor (heartbeat,
    stale kill, interrupt abort) lives on the instance, mutated from both threads."""

    def __init__(self, agent, api_kwargs: dict, on_first_delta):
        self.agent = agent
        self.api_kwargs = api_kwargs
        self.on_first_delta = on_first_delta
        self.worker = None  # request thread; None in inline mode
        self.result = {"response": None, "error": None, "partial_tool_names": []}
        self.clients = _RequestClientRegistry(agent)
        # Request-local cancel flag: the worker recognizes its own interrupt
        # force-close (RemoteProtocolError) and exits instead of retrying (#6600).
        self._request_cancelled = {"value": False}
        self.first_delta_fired = {"done": False}
        self.deltas_were_sent = {"yes": False}  # for the partial-delivery fallback
        self.provider_tool_in_flight = {"yes": False}
        # Last REAL chunk; the monitor detects SSE-ping-only connections with it.
        self.last_chunk_time = {"t": time.time()}
        # Shared by the socket read timeout (``_stream_timeouts``) and the stale
        # detector (``_resolve_stale_timeout``); None until resolved.
        self._stream_stale_timeout = None
        self.stream_attempt_lock = threading.Lock()
        self.stream_attempt_state = {
            "current": 0,
            "cancelled": set(),
            "discarded_chunks": 0,
            "discarded_bytes": 0,
        }
        self.managed_stream_holder = {"stream": None}
        # Per-attempt: single-writer token, request-local client, raw HTTP response (chat wire).
        self._writer_token = self._attempt_request_client = self._attempt_stream_response = None
        # The route ``api_kwargs`` was assembled for; a retry must not replay it on another one.
        self._request_route = self._live_route()

    # ── shared small helpers ────────────────────────────────────────────

    def _live_route(self) -> tuple:
        agent = self.agent
        return tuple(
            str(getattr(agent, attr, "") or "")
            for attr in ("model", "provider", "base_url", "api_mode")
        )

    def _route_switched_under_request(self) -> bool:
        """True once ``/model`` (``switch_model``) re-pointed the agent while this request was in
        flight. The captured payload names the OLD model and is shaped for the OLD provider, but
        every (re)open builds its client from the LIVE agent, so a retry would send a foreign model
        slug to the new base_url (404 + a rate-limit hold, #112121). The turn loop rebuilds the
        request for the current route on its own next attempt, so hand the error back to it.
        """
        live = self._live_route()
        if live == self._request_route:
            return False
        logger.warning(
            "Stream retry skipped: model/provider switched mid-request (%s via %s -> %s via %s); "
            "handing back to the turn loop to rebuild the request for the current route.",
            self._request_route[0],
            self._request_route[1] or self._request_route[2],
            live[0],
            live[1] or live[2],
        )
        return True

    @staticmethod
    def _quiet(fn, *args) -> None:
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
        self.provider_tool_in_flight["yes"] = False
        # Attempt-local like provider_tool_in_flight: a tool name from a stream that died
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

    def _discard_stale_stream_chunk(self, stream_attempt_id: int, chunk) -> None:
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

    def _fire_first_delta(self):
        if not self.first_delta_fired["done"] and self.on_first_delta:
            self.first_delta_fired["done"] = True
            self._quiet(self.on_first_delta)

    def _emit_text(self, text: str) -> None:
        self._fire_first_delta()
        self.agent._fire_stream_delta(text)
        self.deltas_were_sent["yes"] = True

    def _visible_text_delivered(self) -> bool:
        """True when visible assistant text actually reached a stream consumer this attempt
        (``_fire_stream_delta`` records only scrubbed, delivered text; ``deltas_were_sent``
        flips on any content delta, including whitespace/think-only ones nobody saw)."""
        return bool((getattr(self.agent, "_current_streamed_assistant_text", "") or "").strip())

    def _emit_reasoning(self, text: str) -> None:
        self._fire_first_delta()
        self.agent._fire_reasoning_delta(text)

    def _emit_tool_started(self, name: str) -> None:
        self._fire_first_delta()
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

    def _new_diag(self) -> dict:
        diag = self.agent._stream_diag_init()
        self.clients.diag = diag
        return diag

    def _count_chunk(self, diag, chunk) -> None:
        """Stamp liveness for a real chunk; diagnostics are best-effort."""
        self.last_chunk_time["t"] = time.time()
        self.agent._touch_activity("receiving stream response")
        with contextlib.suppress(Exception):
            diag["chunks"] = int(diag.get("chunks", 0)) + 1
            if diag.get("first_chunk_at") is None:
                diag["first_chunk_at"] = self.last_chunk_time["t"]
            # Delta-length estimate: ~3x cheaper than repr() per chunk.
            diag["bytes"] = int(diag.get("bytes", 0)) + _estimate_chunk_bytes(chunk)

    # ── chat_completions wire ───────────────────────────────────────────

    def _stream_timeouts(self) -> tuple[float, float, float]:
        """``(write, read, connect/pool)`` socket timeouts. Per-provider
        ``request_timeout_seconds`` wins over MERTINA_API_TIMEOUT (1800s) and
        MERTINA_STREAM_READ_TIMEOUT (120s); connect/pool cover the handshake, not
        inference: 30s, or capped at 60s when configured."""
        cfg = get_provider_request_timeout(self.agent.provider, self.agent.model)
        base = cfg if cfg is not None else env_float("MERTINA_API_TIMEOUT", 1800.0)
        if cfg is not None:
            return base, cfg, min(base, 60.0)
        read = env_float("MERTINA_STREAM_READ_TIMEOUT", 120.0)
        stale = self._stream_stale_timeout
        if read == 120.0 and self.agent.base_url and is_local_endpoint(self.agent.base_url):
            read = base  # local providers prefill for minutes
            logger.debug(
                "Local provider detected (%s) — stream read timeout raised to %.0fs",
                self.agent.base_url,
                read,
            )
        elif read == 120.0 and stale is not None and stale != float("inf") and stale > read:
            # Reasoning models pause mid-stream for minutes; the stale detector
            # tolerates that, so the raw read timeout must not fire first.
            read = stale
            logger.debug(
                "Cloud reasoning stream — read timeout raised to %.0fs to match stale-stream detector",
                read,
            )
        return base, read, 30.0

    @staticmethod
    def _choiceless_chunk(chunk, finish_reason):
        """Chunk with empty ``choices`` -> ``(usage, finish_reason)``. Raises
        ProviderStreamError for providers (DeepInfra) that send validation errors
        as in-stream chunks (choices=None + error_type/error_message), which
        would otherwise surface as a misleading EmptyStreamError plus retries."""
        usage = (
            chunk.usage if hasattr(chunk, "usage") and chunk.usage else None
        )  # final usage chunk
        # Without this check the error is silently dropped and the stream ends empty → EmptyStreamError →
        # misleading "empty stream" message and pointless retries on the same bad request. (#65631)
        _err_type = getattr(chunk, "error_type", None)
        _err_msg = getattr(chunk, "error_message", None)
        if _err_type or _err_msg:
            _status = _status_code_from_payload(
                {"code": _err_type, "message": _err_msg}
            ) or _status_code_from_value(_err_type)
            body = _provider_error_body(
                {
                    "code": _err_type or "provider_in_stream_error",
                    "message": str(_err_msg or chunk),
                },
                _status,
            )
            raise ProviderStreamError(
                status_code=_status, body=body, raw_text=f"{_err_type}: {_err_msg}"
            )
        # Nous Portal usage frames (choices=[] + lastOne=true, no [DONE]) are a
        # clean terminal, not a drop; relabelled upstreams send 1 / "true".
        # See #90848.
        last_one = getattr(chunk, "lastOne", None)
        if last_one is None and isinstance(getattr(chunk, "model_extra", None), dict):
            last_one = chunk.model_extra.get("lastOne")
        if last_one in (True, 1, "true") and finish_reason is None:
            finish_reason = "stop"
        return usage, finish_reason

    def _open_chat_stream(self, stream_kwargs: dict[str, Any]):
        # Native Gemini rejects OpenAI's usage-streaming extension; so do strict endpoints that
        # already 4xx'd on it this session (``_stream_options_unsupported``, see #9705).
        if not is_native_gemini_base_url(self.agent.base_url) and not getattr(
            self.agent, "_stream_options_unsupported", False
        ):
            stream_kwargs["stream_options"] = {"include_usage": True}
        request_client = self._attempt_request_client = self.clients.set_client(
            self.agent._create_request_openai_client(
                reason="chat_completion_stream_request", api_kwargs=stream_kwargs
            )
        )
        self.last_chunk_time["t"] = time.time()
        self.agent._touch_activity("waiting for provider response (streaming)")
        # #93650: as above — the streaming path carries the same bulk
        # messages/tools payload and pays the same client-side walk.
        stream_kwargs = bypass_chat_sdk_request_transform(stream_kwargs, request_client)
        return request_client.chat.completions.create(**stream_kwargs)

    def _chat_stream_created(self, raw_stream: Any) -> None:
        response = self._attempt_stream_response = getattr(raw_stream, "response", None)
        self.agent._capture_rate_limits(response)
        self.agent._capture_credits(response)
        self.agent._capture_nous_model_switch(response)
        self.agent._stream_diag_capture_response(self.clients.diag, response)
        self.agent._check_openrouter_cache_status(response)
        self._writer_token = claim_stream_writer(self.agent)
        self._reabort_if_cancelled(response)

    def _reabort_if_cancelled(self, response: Any) -> None:
        """Interrupt/stale abort that raced ``create()``: the one-shot pool sweep ran while
        the connect/TLS window held no socket yet (``tcp_force_closed=0``), so nothing stopped
        the request once it came up and the serve kept generating into a dropped consumer
        (#98974). Response headers prove the socket exists now — shut it down (shutdown-only,
        never a cross-thread close) so the worker unwinds as after a stale kill."""
        with self.stream_attempt_lock:
            current = int(self.stream_attempt_state["current"])
            cancelled = (
                self._request_cancelled["value"]
                or current in self.stream_attempt_state["cancelled"]
            )
        if not cancelled:
            return
        self._shutdown_stale_attempt_socket(response)
        if self._attempt_request_client is not None:
            # Kind-aware: the anthropic_messages wire (incl. anthropic-compatible custom endpoints)
            # runs on a request-local Anthropic client with its own slot sweep.
            abort = (
                self.agent._abort_request_anthropic_client
                if self.agent.api_mode == "anthropic_messages"
                else self.agent._abort_request_openai_client
            )
            abort(self._attempt_request_client, reason="cancelled_attempt_late_connect")

    def _accept_chat_chunk(self, stream_attempt_id: int, chunk: Any) -> bool:
        with contextlib.suppress(Exception):
            choices = getattr(chunk, "choices", None)
            choice = choices[0] if choices else None
            delta = getattr(choice, "delta", None)
            # A stale-attempt fence can win while Relay hands back a tool-call chunk: record
            # the in-flight tool call (retry policy must not see a partial text response).
            if getattr(delta, "tool_calls", None):
                self.provider_tool_in_flight["yes"] = True
            # Marker-only finish chunk (no writable delta) always passes: the fence only stops
            # MORE text; fending the completion signal would mislabel a clean end as a drop.
            if getattr(choice, "finish_reason", None) and not any(
                getattr(delta, attr, None)
                for attr in ("content", "tool_calls", "reasoning_content", "reasoning")
            ):
                return True
        if not self._stream_attempt_is_active(stream_attempt_id):
            return False
        if not self._writer_still_current("Streaming"):
            return False
        # Stamp BEFORE Relay processes the chunk so the watchdog can't cancel
        # a live stream mid-interceptor.
        self.last_chunk_time["t"] = time.time()
        return True

    def _writer_still_current(self, label: str) -> bool:
        """Single-writer fence: False (with a warning) once a newer stream claimed the writer slot."""
        token = self._writer_token
        if token is None or stream_writer_is_current(self.agent, token):
            return True
        logger.warning(
            "%s attempt superseded by a newer stream; stopping consumption to preserve the "
            "single-writer invariant (model=%s).",
            label,
            self.api_kwargs.get("model", "unknown"),
        )
        return False

    def _call_chat_completions(self, stream_attempt_id: int):
        """Stream a chat completions response."""
        import httpx as _httpx

        base_timeout, read_timeout, conn_cap = self._stream_timeouts()
        content_parts: list = []
        reasoning_parts: list = []
        # OpenAI structured refusal (``delta.refusal``): the explanation streams here and
        # ``delta.content`` stays empty, so an un-accumulated refusal looks like an empty
        # stream and burns the empty-response retries (the non-streaming fix is #46013).
        refusal_parts: list[str] = []
        reasoning_details: list = []  # OpenRouter replay data (signatures, encrypted blocks)
        pending_text_parts: list[str] = []
        tool_calls = _ToolCallAccumulator()
        tool_calls_acc = tool_calls.acc
        finish_reason = model_name = usage_obj = None
        response_id = upstream_provider = (
            None  # the provider's own id / serving upstream, from the chunks
        )
        role = "assistant"
        _diag = self._new_diag()
        self._writer_token = self._attempt_request_client = self._attempt_stream_response = None
        from mertina.agent.chat_completion_helpers_relay import RelayChatAccumulator

        relay_response = RelayChatAccumulator()

        def _open_stream(next_api_kwargs: dict[str, Any]):
            timeout = _httpx.Timeout(
                connect=conn_cap, read=read_timeout, write=base_timeout, pool=conn_cap
            )
            return self._open_chat_stream({**next_api_kwargs, "stream": True, "timeout": timeout})

        def _flush_pending_stream_text():
            pending_parts = list(pending_text_parts)
            pending_text_parts.clear()
            for text in pending_parts:
                (self._route_suppressed_text if tool_calls_acc else self._emit_text)(text)

        from mertina.agent import relay_llm

        stream = self._set_managed_stream(
            relay_llm.stream(
                self.api_kwargs,
                _open_stream,
                **_relay_stream_identity(self.agent, "provider"),
                finalizer=relay_response.finalize,
                on_stream_created=self._chat_stream_created,
                on_chunk=relay_response.observe,
                accept_chunk=lambda chunk: self._accept_chat_chunk(stream_attempt_id, chunk),
                completed_response_predicate=lambda value: hasattr(value, "choices"),
                metadata=_relay_stream_metadata(self.agent, "chat_completions"),
                defer_logical_completion=True,
            )
        )
        if self.agent.provider == "moa":
            # Mertina interrupts the managed stream; Relay alone closes the provider stream.
            self.clients.set_stream_handle(stream)

        for chunk in _iter_provider_stream_chunks(
            stream, response=lambda: self._attempt_stream_response
        ):
            self._count_chunk(_diag, chunk)
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
            if (
                upstream_provider is None
                and isinstance(getattr(chunk, "provider", None), str)
                and chunk.provider
            ):
                upstream_provider = chunk.provider  # OpenRouter stamps who served
            if not chunk.choices:
                usage, finish_reason = self._choiceless_chunk(chunk, finish_reason)
                usage_obj = usage or usage_obj
                continue

            choice = chunk.choices[0]
            delta = choice.delta
            # Read finish_reason/usage BEFORE any content-shape `continue`: the SSE-echo
            # guard can swallow a merged finish chunk (vLLM standalone ':' tokens).
            finish_reason = (
                _normalize_finish_reason(getattr(choice, "finish_reason", None)) or finish_reason
            )
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
                # Summary-part models omit the separator between markdown blocks; re-insert it.
                reasoning_text = separate_glued_reasoning_blocks(
                    reasoning_parts[-1] if reasoning_parts else "", reasoning_text
                )
                reasoning_parts.append(reasoning_text)
                self._emit_reasoning(reasoning_text)
            # Structured reasoning_details deltas carry the provider's replay data; the
            # non-streaming path already keeps them, so dropping them here lost
            # reasoning continuity on nearly every turn. Pydantic parks unknown fields
            # in ``model_extra``.
            rd_delta = getattr(delta, "reasoning_details", None)
            if rd_delta is None and isinstance(getattr(delta, "model_extra", None), dict):
                rd_delta = delta.model_extra.get("reasoning_details")
            for rd in rd_delta if isinstance(rd_delta, (list, tuple)) else ():
                append_streamed_reasoning_detail(reasoning_details, rd)
            # Not routed to the live display: the transport promotes a sole-payload
            # refusal to content + ``content_filter`` and the loop surfaces it terminally.
            delta_refusal = getattr(delta, "refusal", None)
            if delta_refusal is None and isinstance(getattr(delta, "model_extra", None), dict):
                delta_refusal = delta.model_extra.get("refusal")
            if isinstance(delta_refusal, str) and delta_refusal:
                refusal_parts.append(delta_refusal)

            # Text (list-of-blocks deltas flattened once); possible echoed SSE is
            # buffered until it can be judged.
            delta_content = flatten_message_text(getattr(delta, "content", None), sep="")
            if delta_content:
                content_parts.append(delta_content)
                if tool_calls_acc:
                    self._route_suppressed_text(delta_content)
                elif (
                    pending_text_parts
                    or _provider_stream_text_may_be_sse(delta_content)
                    # A shim cannot follow text already released to the display, so the
                    # whole-content re-join runs only until the first emitted delta.
                    or (
                        not self.deltas_were_sent["yes"]
                        and router_timeout_shim_may_follow("".join(content_parts))
                    )
                ):
                    pending_text_parts.append(delta_content)
                    pending = "".join(pending_text_parts)
                    if not (
                        _provider_stream_text_may_be_sse(pending)
                        or router_timeout_shim_may_follow(pending)
                    ):
                        _flush_pending_stream_text()
                    continue
                else:
                    self._emit_text(delta_content)

            delta_tool_calls = getattr(delta, "tool_calls", None)
            if delta_tool_calls:
                _flush_pending_stream_text()
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
        if stream.final_response is not None:
            return self._adopt_final_response(stream.final_response)
        return self._finish_chat_stream(
            stream,
            role,
            content_parts,
            reasoning_parts,
            tool_calls_acc,
            finish_reason,
            model_name,
            usage_obj,
            flush_pending=_flush_pending_stream_text,
            response_id=response_id,
            upstream_provider=upstream_provider,
            reasoning_details=reasoning_details,
            refusal_parts=refusal_parts,
        )

    def _adopt_final_response(self, final_response):
        """Adapter returned a completed response for ``stream=True``: switch the
        session to non-streaming and replay its content as deltas."""
        logger.info(
            "Streaming request returned a final response object instead of an iterator; "
            "switching %s/%s to non-streaming for this session.",
            self.agent.provider or "unknown",
            self.agent.model or "unknown",
        )
        self.agent._disable_streaming = True
        choices = final_response.choices
        message = getattr(
            choices[0] if isinstance(choices, (list, tuple)) and choices else None, "message", None
        )
        if message is not None:
            reasoning_text = getattr(message, "reasoning_content", None) or getattr(
                message, "reasoning", None
            )
            if reasoning_text is None and isinstance(getattr(message, "model_extra", None), dict):
                reasoning_text = message.model_extra.get(
                    "reasoning_content"
                ) or message.model_extra.get("reasoning")
            if isinstance(reasoning_text, str) and reasoning_text:
                self._emit_reasoning(reasoning_text)
            content = getattr(message, "content", None)
            if isinstance(content, str) and content:
                self._fire_first_delta()
                self.agent._fire_stream_delta(
                    content
                )  # not _emit_text: deltas_were_sent stays False here
        return final_response

    @staticmethod
    def _assemble_tool_calls(tool_calls_acc, finish_reason):
        """Materialize accumulated tool calls; flag truncated/unrepairable args."""
        mock_tool_calls = []
        has_truncated_tool_args = False
        for idx in sorted(tool_calls_acc):
            tc = tool_calls_acc[idx]
            arguments = tc["function"]["arguments"]
            if arguments and arguments.strip():
                try:
                    json.loads(arguments)
                except json.JSONDecodeError:
                    # Repair before flagging (GLM via Ollama); "{}" = unrepairable.
                    repaired = _repair_tool_call_arguments(arguments, tc["function"]["name"] or "?")
                    if repaired != "{}":
                        arguments = repaired
                    else:
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
        stream,
        role,
        content_parts,
        reasoning_parts,
        tool_calls_acc,
        finish_reason,
        model_name,
        usage_obj,
        *,
        flush_pending,
        response_id=None,
        upstream_provider=None,
        reasoning_details=None,
        refusal_parts=None,
    ):
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
                "Provider returned an empty stream with no finish_reason (possible upstream error or malformed SSE response)."
            )
        if has_truncated_tool_args and finish_reason is None:
            # Partial args WITH finish_reason="length" is a real output cap; with NONE the
            # upstream dropped mid tool-call, and stamping "length" burns 3 useless retries.
            _dropped_names = [
                (tool_calls_acc[idx]["function"]["name"] or "?") for idx in sorted(tool_calls_acc)
            ]
            logger.warning(
                "Stream ended with no finish_reason while a tool call's arguments were still incomplete "
                "(tools=%s); treating as a mid-tool-call stream drop, not an output-length truncation.",
                _dropped_names,
            )
            return _build_partial_stream_stub(
                role,
                full_content,
                full_reasoning,
                model_name,
                usage_obj,
                dropped_tool_names=_dropped_names or None,
            )
        if (
            finish_reason is None
            and (content_parts or reasoning_parts)
            and not tool_calls_acc
            and usage_obj is None
        ):
            # Text-only (or reasoning-only) drop: otherwise the partial text is stamped "stop"
            # and the next step is lost — for reasoning-only, the clean-stop promotion in
            # finish_text_response would then surface a truncated thought as the answer.
            # A usage object proves the provider finished (include_usage's final chunk).
            logger.warning(
                "Stream ended with no finish_reason after delivering text with no tool calls; treating as a mid-stream drop."
            )
            return _build_partial_stream_stub(
                role, full_content, full_reasoning, model_name, usage_obj
            )
        effective_finish_reason = "length" if has_truncated_tool_args else (finish_reason or "stop")
        provider_stream_error = _provider_stream_error_from_text(
            full_content or "", effective_finish_reason, response=getattr(stream, "response", None)
        )
        if provider_stream_error is not None:
            raise provider_stream_error
        message = SimpleNamespace(
            role=role,
            content=full_content,
            tool_calls=mock_tool_calls,
            reasoning_content=full_reasoning,
            # ``normalize_response`` reads ``message.refusal`` — same contract as the non-streaming object.
            refusal="".join(refusal_parts or ()) or None,
        )
        if reasoning_details:
            # Only when present: _build_assistant_message's passthrough persists them
            # for replay, and non-reasoning providers keep the attribute absent.
            message.reasoning_details = reasoning_details
        # The provider's id when the chunks carried one (chatcmpl-/gen-...): it is what a provider needs to
        # look a request up. Fabricated only when the stream never sent one.
        response = SimpleNamespace(
            id=response_id or ("stream-" + str(uuid.uuid4())),
            model=model_name,
            usage=usage_obj,
            provider=upstream_provider,
            choices=[
                SimpleNamespace(index=0, message=message, finish_reason=effective_finish_reason)
            ],
        )
        # A held router timeout shim (#68396) is rejected by validate_response and retried;
        # releasing its text here would show the provider failure as assistant output.
        if not is_router_timeout_shim(response):
            flush_pending()
        return response

    # ── anthropic_messages wire ─────────────────────────────────────────

    @staticmethod
    def _check_anthropic_message(message, *, tool_drop: bool = True):
        """Raise EmptyStreamError for a message the stream never completed: no
        content and no stop_reason (eventless -> retry), or with ``tool_drop`` a
        ``tool_use`` block and no stop_reason — the SSE closed mid tool call and
        its input is a partial snapshot (usually ``{}``), so raising blocks the
        empty-args execution (bounded retry, or stub/continuation after text)."""
        content = getattr(message, "content", None)
        if not content and getattr(message, "stop_reason", None) is None:
            raise EmptyStreamError(
                "Provider returned an empty stream with no stop_reason (possible upstream error or malformed event stream)."
            )
        if (
            tool_drop
            and getattr(message, "stop_reason", None) is None
            and any(getattr(block, "type", None) == "tool_use" for block in content or [])
        ):
            raise EmptyStreamError(
                "Stream ended with no stop_reason while a tool_use block was still incomplete; "
                "treating as a mid-tool-call stream drop (#80498)."
            )
        return message

    def _call_anthropic(self, request_client):
        """Stream an Anthropic Messages API response; fires delta callbacks but
        returns the native Message from get_final_message(). Runs on the
        per-request ``request_client`` so the watchdog can abort this socket
        without closing the shared client mid-flight."""
        has_tool_use = False
        # Eventless stream: the SDK's get_final_message() raises AssertionError (no
        # message_start); shims may fabricate a contentless Message. All -> EmptyStreamError.
        saw_stream_event = False
        self.last_chunk_time["t"] = time.time()
        _diag = self._new_diag()
        self._writer_token = self._attempt_stream_response = None
        self._attempt_request_client = request_client
        _stream_context = {"manager": None, "stream": None}
        base_final_message = None

        from mertina.agent import relay_llm
        from mertina.agent.anthropic_adapter import sanitize_anthropic_kwargs

        accumulator = relay_llm.AnthropicStreamAccumulator()

        def _open_anthropic_stream(next_api_kwargs: dict[str, Any]):
            final_kwargs = dict(next_api_kwargs)
            sanitize_anthropic_kwargs(
                final_kwargs, log_prefix=getattr(self.agent, "log_prefix", "")
            )
            manager = request_client.messages.stream(**final_kwargs)
            _stream_context["manager"] = manager
            return manager.__enter__()

        def _anthropic_stream_created(raw_stream: Any) -> None:
            _stream_context["stream"] = raw_stream
            # Same wiring as the chat_completions wire: MessageStream exposes the httpx response,
            # so the interrupt/stale abort can shut down THIS attempt's socket (#98974).
            response = self._attempt_stream_response = getattr(raw_stream, "response", None)
            # Snapshot response diagnostics now so they survive a stream dying before the first event.
            self._quiet(lambda: self.agent._stream_diag_capture_response(_diag, response))
            self._writer_token = claim_stream_writer(self.agent)
            self._reabort_if_cancelled(response)

        stream = self._set_managed_stream(
            relay_llm.stream(
                self.api_kwargs,
                _open_anthropic_stream,
                **_relay_stream_identity(self.agent, "anthropic"),
                finalizer=accumulator.finalize,
                on_stream_created=_anthropic_stream_created,
                on_chunk=accumulator.observe,
                accept_chunk=lambda _event: self._writer_still_current("Anthropic streaming"),
                metadata=_relay_stream_metadata(self.agent, "anthropic_messages"),
                defer_logical_completion=True,
            )
        )
        try:
            for event in stream:
                saw_stream_event = True
                self._count_chunk(_diag, event)
                if self.agent._interrupt_requested:
                    break
                event_type = getattr(event, "type", None)
                if event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block and getattr(block, "type", None) == "tool_use":
                        has_tool_use = True
                        if getattr(block, "name", None):
                            self._emit_tool_started(block.name)
                            # Same as the chat_completions wire: a stream that dies inside the
                            # tool args is retried (no tool has run yet) instead of stubbed.
                            self.result["partial_tool_names"].append(block.name)
                elif event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    delta_type = getattr(delta, "type", None) if delta else None
                    if delta_type == "text_delta":
                        text = getattr(delta, "text", "")
                        if text and not has_tool_use:
                            self._emit_text(text)
                    elif delta_type == "thinking_delta" and getattr(delta, "thinking", ""):
                        self._emit_reasoning(delta.thinking)
            raw_stream = _stream_context["stream"]
            if not self.agent._interrupt_requested and raw_stream is not None:
                try:
                    base_final_message = raw_stream.get_final_message()
                    # The SDK snapshot keeps only stop_reason/stop_sequence from message_delta; the
                    # refusal's stop_details (category/explanation) survives only in our accumulator.
                    _stop_details = accumulator.finalize().get("stop_details")
                    if (
                        _stop_details is not None
                        and getattr(base_final_message, "stop_details", None) is None
                    ):
                        base_final_message.stop_details = _stop_details
                except AssertionError:
                    if not saw_stream_event:
                        raise EmptyStreamError(
                            "Provider returned an empty stream with no events (possible upstream error or malformed event stream)."
                        ) from None
                    raise
        finally:
            try:
                self._close_managed_stream()
            finally:
                manager = _stream_context["manager"]
                if manager is not None:
                    manager.__exit__(None, None, None)

        if self.agent._interrupt_requested:
            return None
        if base_final_message is not None:
            self._check_anthropic_message(base_final_message, tool_drop=False)
            if not stream.output_modified:
                return self._check_anthropic_message(base_final_message)
        return self._check_anthropic_message(accumulator.response(base_final_message))

    # ── retry loop ──────────────────────────────────────────────────────

    def _retry_after_drop(
        self, e, attempt: int, max_retries: int, *, mid_tool_call: bool, reason: str
    ) -> None:
        """Warn about the drop and tear down the request-local client. Shared
        clients are never closed from inside a request (FD-recycle hazard); the
        OpenAI primary is replaced lazily."""
        self.agent._emit_stream_drop(
            error=e,
            attempt=attempt + 2,
            max_attempts=max_retries + 1,
            mid_tool_call=mid_tool_call,
            diag=self.clients.diag,
        )
        if self.agent._is_provider_stream_parse_error(e):
            from mertina.agent.anthropic_adapter import buffer_anthropic_tool_input

            buffer_anthropic_tool_input(
                self.api_kwargs, getattr(self.agent, "_anthropic_base_url", None)
            )
        self._cancel_current_stream_attempt(reason)
        self.clients.close_once(reason)

    def _maybe_disable_streaming(self, e) -> None:
        """Flip to non-streaming for failures streaming itself cannot survive, or that
        re-streaming can only repeat: the provider rejecting streams outright,
        AnthropicBedrock IAM lacking InvokeModelWithResponseStream, or a gateway answering
        with contentless SSE keepalive frames (a degraded gateway answers every
        streaming request that way, so the retry must change channel to make progress)."""
        if _is_provider_stream_empty_frame_error(e):
            self.agent._disable_streaming = True
            logger.warning(
                "Provider stream returned an empty SSE frame (keepalive, no payload) before any "
                "delta — switching %s/%s to non-streaming for this session.",
                self.agent.provider or "unknown",
                self.agent.model or "unknown",
            )
            # Durable channel, not _buffer_status: this recovery is expected to SUCCEED, and
            # buffered retry chatter is dropped on successful recovery. Fires at most once per
            # session (streaming is off from here on).
            self.agent._emit_warning(
                "⚠️ Provider stream returned an empty keepalive frame — retrying this turn "
                "without streaming (streaming stays off for this session)."
            )
            return
        _err_lower = str(e).lower()
        _is_stream_unsupported = "stream" in _err_lower and "not supported" in _err_lower
        _is_bedrock_stream_denied = False
        if not _is_stream_unsupported and "invokemodelwithresponsestream" in _err_lower:
            # Message pre-check first: importing bedrock_adapter triggers a lazy boto3 install.
            from mertina.agent.bedrock_adapter import is_streaming_access_denied_error

            _is_bedrock_stream_denied = is_streaming_access_denied_error(e)
        if _is_stream_unsupported or _is_bedrock_stream_denied:
            self.agent._disable_streaming = True
            self.agent._safe_print(
                "\n⚠  AWS IAM denied bedrock:InvokeModelWithResponseStream. Switching to non-streaming.\n"
                "   Grant that action to restore streaming output.\n"
                if _is_bedrock_stream_denied
                else "\n⚠  Streaming is not supported for this model/provider. Switching to non-streaming.\n"
                "   To avoid this delay, set display.streaming: false in config.yaml\n",
                diagnostic=True,
            )

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
        # ReadError: abort/reset mid-body (stale-kill shutdown under a parked reader,
        # ECONNRESET) — the retry loop owns recovery.
        _is_conn_err = isinstance(
            e, (_httpx.ConnectError, _httpx.ReadError, _httpx.RemoteProtocolError, ConnectionError)
        )
        _is_stream_parse_err = self.agent._is_provider_stream_parse_error(e)
        _is_empty_stream = isinstance(e, EmptyStreamError)
        _is_sse_conn_err = not _is_timeout and not _is_conn_err and _is_sse_connection_error(e)
        _is_transient = _is_timeout or _is_conn_err or _is_sse_conn_err or _is_stream_parse_err

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
            _partial_tool_in_flight = (
                bool(self.result.get("partial_tool_names")) or self.provider_tool_in_flight["yes"]
            )
            if not _partial_tool_in_flight and not self._visible_text_delivered():
                # Deltas fired but nothing visible reached a consumer (whitespace/think-only
                # deltas, or no display consumer at all) and no tool call is in flight: from
                # the user's and the model's point of view NOTHING was delivered. The
                # "partial delivery" stub would be EMPTY and the loop would ask the model to
                # continue from nowhere, so it repeats the lost step (#112419). Classify as an
                # undelivered failure instead: same-prefix retry, then the main loop's
                # fallback/backoff — there is no text to duplicate.
                logger.warning(
                    "Stream died after deltas but before any visible text was delivered (0 chars, "
                    "no tool call in flight); treating as an undelivered stream failure: %s",
                    e,
                )
                self._quiet(self.agent._reset_stream_delivery_tracking)
                self.deltas_were_sent["yes"] = False
                self.first_delta_fired["done"] = False
        if self.deltas_were_sent["yes"]:
            # Died AFTER tokens were delivered: normally no retry (would duplicate
            # text). Exception: a tool call in flight — aborting discards it, so
            # retry TRANSIENT errors (a "reconnecting" marker + duplicated
            # preamble beats a failed action; no tool has executed yet).
            if not (_partial_tool_in_flight and _is_transient and attempt < max_retries):
                logger.warning("Streaming failed after partial delivery, not retrying: %s", e)
                self.result["error"] = e
                return False
            # Marker explains the re-streamed preamble (``_emit_stream_drop`` logs the WARNING);
            # reset the streamed-text buffer so it isn't double-recorded; fresh accumulators.
            if self.agent._warning_presentation_enabled():
                self._quiet(
                    self.agent._fire_stream_delta,
                    "\n\n⚠ Connection dropped mid tool-call; reconnecting…\n\n",
                )
            self._quiet(self.agent._reset_stream_delivery_tracking)
            self.deltas_were_sent["yes"] = False
            self.first_delta_fired["done"] = False
            self._retry_after_drop(
                e, attempt, max_retries, mid_tool_call=True, reason="stream_mid_tool_retry_cleanup"
            )
            return True

        if _is_transient or _is_empty_stream:
            # Transient network / timeout error: retry with a fresh connection first.
            if attempt < max_retries:
                self._retry_after_drop(
                    e, attempt, max_retries, mid_tool_call=False, reason="stream_retry_cleanup"
                )
                return True
            # Exhausted: log full diagnostics (chain, headers, bytes/elapsed).
            self.agent._log_stream_retry(
                kind="exhausted",
                error=e,
                attempt=max_retries + 1,
                max_attempts=max_retries + 1,
                mid_tool_call=False,
                diag=self.clients.diag,
            )
            # Empty stream: "connection failed" would send users chasing network issues.
            if _is_stream_parse_err or _is_empty_stream:
                _what = (
                    "Provider returned malformed streaming data after"
                    if _is_stream_parse_err
                    else "Provider returned an empty response stream after"
                )
                self.agent._buffer_diagnostic_status(
                    f"❌ {_what} {max_retries + 1} attempts. The provider may be experiencing issues — try again in a moment."
                )
            else:
                from mertina.agent.stream_diag import buffer_connect_exhausted_notice

                buffer_connect_exhausted_notice(
                    self.agent, e, attempts=max_retries + 1, base_url=self.agent.base_url
                )
        else:
            self._maybe_disable_streaming(e)
            logger.exception("Streaming failed before delivery: %s", e)
        # Propagate to the main retry loop (credential rotation, fallback, backoff).
        self.result["error"] = e
        return False

    def _call_wire(self, stream_attempt_id: int):
        if self.agent.api_mode != "anthropic_messages":
            return self._call_chat_completions(stream_attempt_id)
        # Per-request client so the watchdog aborts its socket, not the shared one.
        request_client = self.clients.set_client(
            self.agent._create_request_anthropic_client(reason="anthropic_stream_request"),
            kind="anthropic_messages",
        )
        return self._call_anthropic(request_client)

    def _call(self):
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
                        self.agent, lambda: self._call_wire(stream_attempt_id)
                    )
                    return  # success
                except Exception as e:
                    self._close_managed_stream()
                    if not self._handle_stream_error(e, _stream_attempt, _max_stream_retries):
                        return
                    if self._route_switched_under_request():
                        self.result["error"] = e
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

    # ── poll-loop monitor (heartbeat / stale kill / interrupt) ──────────

    def _run_call(self):
        try:
            self._call()
        finally:
            self._call_done.set()

    def _shutdown_stale_attempt_socket(self, response: Any) -> None:
        """Best-effort ``shutdown()`` on the killed attempt's socket (monitor thread).

        The pool sweep in ``close_once`` can miss a connection that is checked
        out for the in-flight body read. ``shutdown(SHUT_RDWR)`` is FD-safe
        from any thread — it wakes the owner's ``recv`` without releasing the
        descriptor — so the worker unwinds and releases its own response on
        the owner thread (``_call``'s ``except``/``finally``). Never
        ``close()`` here: releasing a live TLS descriptor from a stranger
        thread lets the kernel recycle it under the owner's SSL BIO, which is
        exactly what the shutdown-only rule in ``_abort_request_slot_client``
        forbids (it covers request-local clients too, #30858).
        """
        if response is None or response is not self._attempt_stream_response:
            return
        try:
            from mertina.agent.agent_runtime_helpers import (
                _connection_candidates,
                _shutdown_socket,
                _socket_from_candidate,
            )

            exts = getattr(response, "extensions", None) or {}
            direct = exts.get("network_stream") if isinstance(exts, dict) else None
            for start in (direct, getattr(response, "stream", None)):
                if start is None:
                    continue
                for candidate in _connection_candidates(start):
                    sock = _socket_from_candidate(candidate)
                    if sock is None:
                        continue
                    _shutdown_socket(sock)
                    logger.info(
                        "Shut down the stale stream's socket to unblock the reader "
                        "(attempt superseded; model=%s).",
                        self.api_kwargs.get("model", "unknown"),
                    )
                    return
            logger.debug(
                "Stale stream socket shutdown found no socket; pool sweep is the only abort"
            )
        except Exception:
            logger.debug("Stale stream socket shutdown failed", exc_info=True)

    def _kill_stale_stream(self, elapsed: float) -> None:
        """SSE pings but no chunks: cancel the attempt and abort the request-local
        client so the retry loop opens a fresh one. The shared client is never
        closed from this (stranger) thread — earlier stale-killed workers may
        still be unwinding SSL BIOs (FD-recycle corruption); the OpenAI primary
        is replaced lazily."""
        _est_ctx = estimate_request_context_tokens(self.api_kwargs)
        logger.warning(
            "Stream stale for %.0fs (threshold %.0fs) — no chunks received. model=%s context=~%s tokens. Killing connection.",
            elapsed,
            self._stream_stale_timeout,
            self.api_kwargs.get("model", "unknown"),
            f"{_est_ctx:,}",
        )
        self.agent._buffer_diagnostic_status(
            f"⚠️ No response from provider for {int(elapsed)}s (model: {self.api_kwargs.get('model', 'unknown')}, "
            f"context: ~{_est_ctx:,} tokens). Reconnecting..."
        )
        # Captured BEFORE the cancel/abort: the pool sweep can miss a checked-out
        # connection, so shut down the killed attempt's own socket too — still
        # shutdown-only, never close (see the helper).
        _killed_response = self._attempt_stream_response
        with contextlib.suppress(Exception):
            self._cancel_current_stream_attempt("stale_stream_kill")
            self.clients.close_once("stale_stream_kill")
        self._shutdown_stale_attempt_socket(_killed_response)
        _bump_stale_streak(self.agent)  # circuit breaker, see ``_stale_streak()``
        # Reset the timer so we don't kill repeatedly while the worker unwinds.
        self.last_chunk_time["t"] = time.time()
        self.agent._emit_diagnostic_wait(
            f"⚠ no output from provider for {int(elapsed)}s — reconnecting..."
        )
        self.agent._touch_activity(f"stale stream detected after {int(elapsed)}s, reconnecting")

    def _abort_for_interrupt(self, stale_elapsed: float) -> None:
        """/stop seen by the monitor: mark cancelled, abort the request-local
        socket, wait for the worker, flag the interrupt."""
        # The stale branch already counted this iteration if its deadline won the race.
        if stale_elapsed <= self._stream_stale_timeout:
            _record_interrupted_provider_wait(
                self.agent, stale_elapsed, response_started=self.deltas_were_sent["yes"]
            )
        # Mark cancelled BEFORE force-closing so the worker treats the forced
        # transport error as a cancel, not a network error (#6600).
        self._request_cancelled["value"] = True
        logger.debug("Force-closing streaming httpx client due to interrupt (not a network error).")
        # Same as the stale kill: the pool sweep can miss the connection checked out for the
        # in-flight body read, so shut down the attempt's own socket too (#98974).
        _killed_response = self._attempt_stream_response
        with contextlib.suppress(Exception):
            self._cancel_current_stream_attempt("stream_interrupt_abort")
            # Kind-aware: only the request-local socket; the shared _anthropic_client is never closed here.
            self.clients.close_once("stream_interrupt_abort")
        self._shutdown_stale_attempt_socket(_killed_response)
        # Let the worker unwind Relay-managed scopes first; raising first lets
        # turn teardown race a still-open scope and corrupt the LIFO stack.
        if self.worker is not None:
            _join_worker_for_relay_teardown(self.worker, label="Streaming")
        self._monitor_interrupted["yes"] = True

    # ── orchestration ───────────────────────────────────────────────────

    def _resolve_stale_timeout(self) -> None:
        """Set ``_stream_stale_timeout``. Local endpoints (unless the env is set) get
        long but FINITE patience — 900s / ``agent.local_stream_stale_timeout`` /
        MERTINA_LOCAL_STREAM_STALE_TIMEOUT — an infinite one stalled sessions on a
        crashed endpoint forever. Cloud values scale with context size and are
        floored for known reasoning models (else BrokenPipeError from the gateway)."""
        base = _configured_stale_base(self.agent)
        if base == 180.0 and self.agent.base_url and is_local_endpoint(self.agent.base_url):
            self._stream_stale_timeout = _local_stream_stale_timeout_default()
            logger.debug(
                "Local provider detected (%s) — stale stream timeout set to %.0fs",
                self.agent.base_url,
                self._stream_stale_timeout,
            )
            return
        self._stream_stale_timeout = _cloud_stale_timeout_for(self.agent, self.api_kwargs)

    def _partial_stream_stub(self):
        """Tokens already reached the platform: a finish_reason="length" stub fires the
        continuation machinery; tool_calls=None blocks executing incomplete calls.
        Content may be EMPTY (dropped tool call, overflow) — the loop skips appending an
        empty stub and only sends the nudge (placeholder text leaked into the stitched
        response). A text-only death with 0 visible chars never gets here: the error
        handler reclassifies it as undelivered (#112419)."""
        error = self.result["error"]
        _partial_text = (
            getattr(self.agent, "_current_streamed_assistant_text", "") or ""
        ).strip() or None
        _partial_names = list(self.result.get("partial_tool_names") or [])
        if _partial_names:
            # User-visible warning so the user and model both know what was attempted.
            _name_str = ", ".join(_partial_names[:3])
            if len(_partial_names) > 3:
                _name_str += f", +{len(_partial_names) - 3} more"
            _warn = (
                f"\n\n⚠ Stream stalled mid tool-call ({_name_str}); the action was not executed. "
                f"Ask me to retry if you want to continue."
            )
            _partial_text = (_partial_text or "") + _warn  # model/result bookkeeping, never gated
            if self.agent._warning_presentation_enabled():
                self._quiet(self.agent._fire_stream_delta, _warn)  # visible immediately
            logger.warning(
                "Partial stream dropped tool call(s) %s after %s chars of text; surfaced warning to user: %s",
                _partial_names,
                len(_partial_text or ""),
                error,
            )
        # Classify the error before it is swallowed into the stub: the loop reads the
        # content-filter tag and falls back; a context overflow must not be continued at all.
        _cls = None
        with contextlib.suppress(Exception):
            from mertina.agent.error_classifier import classify_api_error

            _cls = classify_api_error(
                error,
                provider=str(getattr(self.agent, "provider", "") or ""),
                model=str(getattr(self.agent, "model", "") or ""),
            )
        _reset_stale_streak(self.agent)  # deltas fired => provider responsive: clear the breaker
        # #106260: continuing after a context-overflow error re-sends a larger request into the
        # same overflow. Return an EMPTY stub marked terminal so the loop ends the turn instead.
        # Scope is context_overflow ONLY: payload_too_large (413) has its own byte-scored recovery
        # owner (turn_overflow._recover_payload_too_large, #88960/#47339) that must not be bypassed.
        if _cls is not None and _cls.reason == FailoverReason.context_overflow:
            logger.warning(
                "Partial stream ended on a context-overflow error after %s chars; "
                "NOT seeding a continuation stub (transcript is already over budget): %s",
                len(_partial_text or ""),
                error,
            )
            return _build_partial_stream_stub(
                "assistant",
                None,
                None,
                getattr(self.agent, "model", "unknown"),
                None,
                dropped_tool_names=_partial_names,
                overflow_terminal=True,
            )
        if not _partial_names:
            logger.warning(
                "Partial stream delivered before error; returning length-truncated stub with %s chars of "
                "recovered content so the loop can continue from where the stream died: %s",
                len(_partial_text or ""),
                error,
            )
        _stub = _build_partial_stream_stub(
            "assistant",
            _partial_text,
            None,
            getattr(self.agent, "model", "unknown"),
            None,
            dropped_tool_names=_partial_names,
        )
        if _cls is not None and _cls.reason == FailoverReason.content_policy_blocked:
            _stub._content_filter_terminated = True
        return _stub

    def run(self):
        """Resolve the stale timeout, run the request (worker thread or inline),
        drive the heartbeat/stale/interrupt monitor, then translate the outcome."""
        self._resolve_stale_timeout()
        # Delegated children and cron turns run the request INLINE (a worker inside
        # their nested pools wedges before the socket opens) but must still STREAM
        # (edge proxies kill silent POSTs). Only the poll loop moves to a monitor
        # thread, which never issues a request, so the no-worker deadlock fix holds.
        self._call_done = threading.Event()
        self._monitor_interrupted = {"yes": False}
        if should_use_direct_api_call(self.agent):
            self.worker = None
            monitor = threading.Thread(
                target=_context_thread_target(self._monitor_loop),
                name="stream-inline-monitor",
                daemon=True,
            )
            monitor.start()
            try:
                self._run_call()
            finally:
                monitor.join(timeout=2.0)
        else:
            self.worker = threading.Thread(
                target=_context_thread_target(self._run_call), daemon=True
            )
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
        if self.result["response"] is not None:
            _reset_stale_streak(self.agent)  # provider proved responsive: clear the breaker
        # Propagate first-chunk timing for the ``post_api_request`` hook.
        if isinstance(self.clients.diag, dict) and self.clients.diag.get("first_chunk_at"):
            self.agent._last_api_first_chunk_at = float(self.clients.diag["first_chunk_at"])
        return self.result["response"]


def interruptible_streaming_api_call(agent, api_kwargs: dict, *, on_first_delta=None):
    """Streaming variant of _interruptible_api_call: fires the delta callbacks per
    text token (tool-call turns suppress them) and returns a SimpleNamespace in
    the non-streaming response shape. codex_responses delegates to the already-
    streaming codex runner; cron turns and delegated children run inline."""
    if agent._interrupt_requested:
        raise InterruptedError("Agent interrupted before streaming API call")
    if agent.api_mode == "codex_responses":
        return _stream_codex_passthrough(agent, api_kwargs, on_first_delta)
    if agent.api_mode == "bedrock_converse":
        return _BedrockStream(agent, api_kwargs, on_first_delta).run()
    # Cross-turn stale-stream circuit breaker (see ``_stale_streak()``).
    _check_stale_giveup(agent)
    return _StreamingCall(agent, api_kwargs, on_first_delta).run()
