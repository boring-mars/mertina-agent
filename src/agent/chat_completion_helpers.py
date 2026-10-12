"""API-call helpers extracted from :class:`AIAgent`: non-streaming and streaming
request drivers, request kwargs builder, assistant-message materializer,
provider-fallback activator, max-iterations handler, per-turn resource cleanup.

Each function takes the parent ``AIAgent`` as ``agent``; AIAgent keeps thin
forwarders. Symbols tests patch on ``run_agent`` (``cleanup_vm`` /
``cleanup_browser``) are resolved through :func:`_ra` at call time.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Optional

from hermes_cli.timeouts import get_provider_request_timeout, get_provider_stale_timeout
from hermes_constants import PARTIAL_STREAM_STUB_ID, FINISH_REASON_LENGTH
from agent.error_classifier import (
    PROVIDER_STREAM_EMPTY_FRAME_ERROR_CODE, PROVIDER_STREAM_NON_JSON_ERROR_CODE)
from agent.sdk_transform_bypass import bypass_chat_sdk_request_transform
from agent.errors import EmptyStreamError
from agent.chat_completion_stream_monitor import StreamingWaitMonitor
from agent.transports.chat_completions import is_router_timeout_shim, router_timeout_shim_may_follow
from agent.fast_mode import effective_request_overrides
from agent.gemini_native_adapter import is_native_gemini_base_url
# Remote endpoints must never be fingerprinted: the probe waterfall is only valid for local/LM-Studio/Ollama
# boxes. Non-Ollama remotes (sglang, vLLM, OpenAI-compat) expose Ollama-compat endpoints that can
# misidentify and, without an api_key, return 401 on every leg (issue #89863).
from agent.model_metadata import is_local_endpoint
from agent.message_content import flatten_message_text
from agent.message_metadata import stamp_message_timestamp
from agent.message_sanitization import (
    _sanitize_surrogates, _repair_tool_call_arguments, normalize_finish_reason as _normalize_finish_reason,
)
from agent.reasoning_summaries import append_streamed_reasoning_detail, separate_glued_reasoning_blocks
from agent.stream_single_writer import claim_stream_writer, stream_writer_is_current
from utils import base_url_host_matches, env_float, env_int

logger = logging.getLogger(__name__)
_OPENROUTER_PROVIDER_SORT_VALUES = {"throughput", "latency", "price"}
_PROVIDER_STREAM_ERROR_FINISH_REASONS = {"error", "error_finish"}
_PROVIDER_STREAM_SSE_FIELDS = {"event", "data", "id", "retry"}
_PROVIDER_STREAM_ERROR_TEXT_LIMIT = 4096

# Fallback chain exhausted on a non-rate-limit failure (#24996): arm a short
# cooldown so the NEXT turn's restore_primary_runtime stays gated instead of
# resetting _fallback_index=0 and re-marshaling the whole context across every
# provider again (memory/swap exhaustion on constrained hosts). Rate-limit /
# billing reasons keep their own longer cooldown.
_FALLBACK_EXHAUSTED_COOLDOWN_S = 5.0


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
    worker.join(timeout=2.0)
    if worker.is_alive():
        logger.warning("%s worker still alive after interrupt abort (2.0s join "
            "timeout); Relay teardown will best-effort drain orphaned scopes (#81521).", label)


def _ra():
    """Lazy ``run_agent`` reference so ``patch("run_agent.cleanup_vm")`` etc. intercept."""
    import run_agent
    return run_agent


class ProviderStreamError(Exception):
    """Provider encoded an API error as streaming content instead of an SDK error."""

    def __init__(self, *, status_code: Optional[int], body: dict, raw_text: str, headers: Any = None):
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


def _status_code_from_value(value: Any) -> Optional[int]:
    if isinstance(value, int) and 100 <= value < 600:
        return value
    if not isinstance(value, str):
        return None
    match = re.search(r"(?:HTTP_STATUS/)?\b([1-5]\d\d)\b", value, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _status_code_from_payload(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None

    candidates = [payload.get(k) for k in ("status_code", "status", "http_status")]
    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        candidates.extend(error_obj.get(k) for k in ("status_code", "status", "http_status", "code"))
    candidates.append(payload.get("code"))
    for candidate in candidates:
        status_code = _status_code_from_value(candidate)
        if status_code is not None:
            return status_code
    return None


def _json_object_from_text(text: str) -> Optional[dict]:
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
            events.append({
                "event": current["event"],
                "data": "\n".join(current["data"]),
                "comments": list(current["comments"]),
                "fields": dict(current["fields"]),
                "status_code": next(
                    (s for s in map(_status_code_from_value, status_candidates) if s is not None), None),
            })
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


def _provider_error_body(payload: dict, status_code: Optional[int]) -> dict:
    """Normalize common provider error payloads to OpenAI-style body.error."""
    if not isinstance(payload, dict):
        payload = {}
    elif isinstance(payload.get("error"), dict):
        return payload
    code = (payload.get("code") or payload.get("error_code") or payload.get("type")
            or (f"HTTP_{status_code}" if status_code else "provider_stream_error"))
    message = (payload.get("message") or payload.get("error_description") or payload.get("error")
               or "Provider stream returned an error event.")
    normalized_error = {"message": str(message)}
    if code:
        normalized_error["code"] = str(code)
    for key in ("request_id", "param", "type"):
        if payload.get(key):
            normalized_error[key] = payload[key]
    return {"error": normalized_error}


def _provider_stream_error_from_json_decode_error(error: json.JSONDecodeError, *,
    response: Any = None) -> ProviderStreamError:
    """Preserve plain-text SSE data rejected inside the OpenAI SDK: on a non-JSON
    ``event: error`` the SDK raises from ``sse.json()`` before yielding a chunk,
    but ``JSONDecodeError.doc`` still carries the provider's original message.

    An EMPTY ``doc`` is the other case: the frame carried no payload at all
    (``data:`` / ``event: ping`` / ``id:`` alone — legal SSE keepalives and no-ops),
    which the SDK's ``json.loads`` rejects the same way. A gateway that is degrading
    answers EVERY streaming request with such frames, so this is not the provider's
    malformed payload and must not be reported as one: it gets its own code and
    the stream helper recovers by retrying without streaming."""
    from agent.redact import redact_sensitive_text
    raw_text = str(getattr(error, "doc", "") or "").strip()
    headers = getattr(response, "headers", None) if response is not None else None
    if not raw_text:
        return ProviderStreamError(
            status_code=None,
            body=_provider_error_body(
                {"code": PROVIDER_STREAM_EMPTY_FRAME_ERROR_CODE,
                    "message": "Provider stream returned an empty SSE data frame (keepalive with no payload)."},
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
            {"code": PROVIDER_STREAM_NON_JSON_ERROR_CODE,
                "message": safe_text or "Provider stream returned non-JSON SSE data."},
            None,
        ),
        raw_text=safe_text,
        headers=headers,
    )




def _iter_provider_stream_chunks(stream, *, response: Any = None):
    """Yield SDK chunks while translating SDK-level SSE decode failures."""
    try:
        yield from stream
    except json.JSONDecodeError as error:
        stream_response = response() if callable(response) else response
        if stream_response is None:
            stream_response = getattr(stream, "response", None)
        raise _provider_stream_error_from_json_decode_error(error, response=stream_response) from error


def _payload_has_error_shape(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("error"), (dict, str)):
        return True
    return bool(payload.get("message")) and bool(
        payload.get("code") or payload.get("error_code") or _status_code_from_payload(payload) is not None)


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
            sse_field.startswith(field_name) for sse_field in _PROVIDER_STREAM_SSE_FIELDS):
            return True
        return False

    return saw_sse_field


def _provider_stream_error_from_text(text: str, finish_reason: Optional[str], *,
    response: Any = None) -> Optional[ProviderStreamError]:
    """Convert provider-streamed error text into an exception for retry logic."""
    if not text:
        return None

    if str(finish_reason or "").lower() not in _PROVIDER_STREAM_ERROR_FINISH_REASONS:
        return None

    headers = getattr(response, "headers", None) if response is not None else None

    def _error(payload: dict, status_code: Optional[int]) -> ProviderStreamError:
        return ProviderStreamError(status_code=status_code, body=_provider_error_body(payload, status_code),
            raw_text=text, headers=headers)

    for event in _parse_provider_sse_events(text):
        is_error_event = str(event.get("event") or "").strip().lower() == "error"
        payload = _json_object_from_text(event.get("data") or "") or {}
        status_code = event.get("status_code") or _status_code_from_payload(payload)
        # The finish_reason is an error here, so an error event always qualifies;
        # a non-error event needs an error-shaped payload or an HTTP error code.
        if (status_code is not None and status_code >= 400) or is_error_event or _payload_has_error_shape(payload):
            return _error(payload, status_code)

    payload = _json_object_from_text(text)
    if payload is not None:
        return _error(payload, _status_code_from_payload(payload))

    if text.strip():
        return _error({}, None)
    return None


_IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})


def _image_part_chars(part: Dict[str, Any], image_cost: int) -> int:
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
        if isinstance(part_type, str) and part_type in _IMAGE_PART_TYPES and any(k in value for k in ("image_url", "image", "source", "file_id")):
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
    from agent.image_token_cost import current_image_token_cost

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
    from agent.codex_responses_adapter import classify_responses_route
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


def _validated_openrouter_provider_sort(raw_sort: Any) -> Optional[str]:
    """Return a normalized OpenRouter provider.sort value or None."""
    if not isinstance(raw_sort, str):
        return None
    sort_value = raw_sort.strip().lower()
    if not sort_value:
        return None
    if sort_value in _OPENROUTER_PROVIDER_SORT_VALUES:
        return sort_value
    logger.warning("Ignoring invalid OpenRouter provider.sort value %r (allowed: %s)", raw_sort,
        ", ".join(sorted(_OPENROUTER_PROVIDER_SORT_VALUES)))
    return None


def _provider_preferences_for_agent(agent) -> Dict[str, Any]:
    """Build the validated provider-routing object shared by request paths.

    ``provider_routing.models.<id>`` overlays the flat constructor values for the CURRENT
    ``agent.model`` (so ``/model`` switches, fallbacks, and delegated children on another
    model each get their own pins without any surface re-plumbing the kwargs)."""
    flat = {"only": agent.providers_allowed, "ignore": agent.providers_ignored, "order": agent.providers_order,
        "sort": agent.provider_sort, "require_parameters": agent.provider_require_parameters,
        "data_collection": agent.provider_data_collection}
    per_model = {}
    with contextlib.suppress(Exception):
        from hermes_cli.config import load_config_readonly
        from hermes_constants import resolve_per_model_provider_routing
        _pr = load_config_readonly().get("provider_routing")
        per_model = resolve_per_model_provider_routing(agent.model, (_pr or {}).get("models") if isinstance(_pr, dict) else None)
    merged = {**flat, **{k: v for k, v in per_model.items() if k in flat}}
    merged["sort"] = _validated_openrouter_provider_sort(merged["sort"])
    merged["require_parameters"] = True if merged["require_parameters"] else None
    return {key: value for key, value in merged.items() if value}


def _prompt_cache_scope_for_agent(agent) -> "str | None":
    return None





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


# ── Cross-turn stale-call circuit breaker (#58962) ─────────────────────
# A session wedged against an unresponsive provider would otherwise hit the
# stale detector on every call forever. ``agent._consecutive_stale_streams``
# is bumped on every stale kill and reset only when a call completes or the
# provider is swapped (switch_model / try_activate_fallback /
# restore_primary_runtime — the streak measured the OLD provider). Past the
# give-up threshold, calls abort immediately with an actionable error.

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
    logger.warning("Interrupted provider wait counted as stale after %.0fs with no output; "
        "consecutive stale attempts=%d.", elapsed, _stale_streak(agent))
    return True


def _report_stale_nonstream_kill(agent, api_kwargs: dict, elapsed: float, stale_timeout: float, *,
    inline: bool = False, hint: Optional[str] = None) -> None:
    """Log + status message for a stale non-streaming kill, shared by the worker
    poll loop and the inline ``direct_api_call`` watchdog (their kill/state
    sequences differ deliberately: different locking models)."""
    model = api_kwargs.get("model", "unknown")
    logger.warning("%son-streaming API call stale for %.0fs (threshold %.0fs). "
        "model=%s context=~%s tokens. Killing connection.", "Inline n" if inline else "N", elapsed,
        stale_timeout, model, f"{estimate_request_context_tokens(api_kwargs):,}")
    try:
        agent._buffer_diagnostic_status(
            f"⚠️ No response from provider for {int(elapsed)}s (non-streaming, model: {model}). {hint or 'Aborting call.'}")
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


def _configured_stale_base(agent) -> float:
    """Per-provider ``stale_timeout_seconds`` config, else HERMES_STREAM_STALE_TIMEOUT (180s)."""
    cfg = get_provider_stale_timeout(agent.provider, agent.model)
    return cfg if cfg is not None else env_float("HERMES_STREAM_STALE_TIMEOUT", 180.0)


def _local_stream_stale_timeout_default() -> float:
    """Local-provider stale ceiling: ``agent.local_stream_stale_timeout`` (900s) or
    HERMES_LOCAL_STREAM_STALE_TIMEOUT. Shared by the stream stale detector and the
    Responses first-event watchdog so both give a local server the same prefill grace."""
    local_default = 900.0
    with contextlib.suppress(Exception):
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()  # read-only consumer — no deepcopy
        agent_cfg = cfg.get("agent") if isinstance(cfg, dict) else None
        value = agent_cfg.get("local_stream_stale_timeout") if isinstance(agent_cfg, dict) else None
        if isinstance(value, (int, float)):
            local_default = float(value)
    return env_float("HERMES_LOCAL_STREAM_STALE_TIMEOUT", local_default)


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
    from agent.reasoning_timeouts import get_reasoning_stale_timeout_floor
    timeout = _scale_stale_timeout_for_context(base, estimate_request_context_tokens(api_kwargs))
    floor = get_reasoning_stale_timeout_floor(api_kwargs.get("model") or api_kwargs.get("modelId") or "")
    if floor is None and api_kwargs.get("modelId"):
        floor = _bedrock_reasoning_stale_floor(api_kwargs["modelId"])
    return timeout if floor is None else max(timeout, floor)




def _cloud_stale_timeout_for(agent, api_kwargs: dict) -> float:
    """An explicit ``providers.<id>.stale_timeout_seconds`` is the operator's deadline and
    wins over every implicit floor — the context-size tier as well as the reasoning-model
    floor — so it can SHORTEN patience for a hung stream (#115024). Only the 180s default
    is scaled and floored."""
    explicit = get_provider_stale_timeout(agent.provider, agent.model)
    if explicit is not None:
        return explicit
    return _cloud_stale_timeout(env_float("HERMES_STREAM_STALE_TIMEOUT", 180.0), api_kwargs)


def _bedrock_reasoning_stale_floor(model_id: object) -> "float | None":
    """Map a Bedrock inference-profile id to its reasoning stale-timeout floor.

    ``us.anthropic.claude-opus-4-6-v1:0`` -> strip the region prefix, then try the
    segment after the provider namespace (``claude-opus-4-6-v1:0``) and the id with
    the provider dot dashed (``deepseek-r1-v1:0``). The floor table mixes dashed
    and dotted versions while Bedrock always dashes, so each candidate is also
    tried with digit-dash-digit <-> digit-dot-digit swapped (version separators
    only). First non-None wins; None for unknown models.
    """
    from agent.reasoning_timeouts import get_reasoning_stale_timeout_floor
    if not model_id or not isinstance(model_id, str):
        return None
    name = model_id.strip().lower()
    for prefix in ("global.", "us.", "eu.", "apac.", "ap.", "au.", "jp.", "ca.", "sa.", "me.", "af."):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    base_candidates = [name]
    if "." in name:
        base_candidates.append(name.rsplit(".", 1)[1])   # claude-opus-4-6-v1:0
        base_candidates.append(name.replace(".", "-", 1))  # deepseek-r1-v1:0
    candidates = dict.fromkeys(
        form for cand in base_candidates
        for form in (cand, re.sub(r"(?<=\d)-(?=\d)", ".", cand), re.sub(r"(?<=\d)\.(?=\d)", "-", cand)))
    return next((f for f in map(get_reasoning_stale_timeout_floor, candidates) if f is not None), None)




def _dispatch_nonstreaming_api_request(agent, api_kwargs: dict, *, make_client):
    """Run one non-streaming Chat Completions request."""
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
    if getattr(agent, "api_mode", None) != "chat_completions" or getattr(agent, "provider", None) == "moa":
        return False
    if getattr(agent, "platform", None) == "cron":
        return True
    # Delegated child — via the execution ContextVar set by _run_single_child,
    # with the agent's platform stamp as a fallback for callers that bypass it.
    with contextlib.suppress(Exception):
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():
            return True
    return getattr(agent, "platform", None) == "subagent"


# How often an in-flight direct_api_call refreshes last_activity_ts. Must stay well
# under the async-delegation idle stall threshold (450s) and below the 30s monitor sweep.
_DIRECT_API_ACTIVITY_HEARTBEAT_SECONDS = 15.0


def _managed_local_load_notice(agent, api_kwargs: dict) -> "Optional[str]":
    """Live phase notice ("⏳ loading <model> into memory — N%" / "⚙ processing
    prompt — P%") while the managed local server works before the first token;
    None when neither applies. Otherwise a cold load reads as a generic stall."""
    try:
        base = str(getattr(agent, "base_url", "") or "")
        if not base:
            return None
        from urllib.parse import urlparse
        from hermes_cli.local_runtime.load_progress import get_loading_progress, get_prefill_progress
        from hermes_cli.local_runtime.supervisor import state_path
        state = json.loads(state_path().read_text(encoding="utf-8"))
        managed = urlparse(str(state.get("base_url", ""))).netloc.lower()
        if not managed or urlparse(base).netloc.lower() != managed:
            return None
        model = str(api_kwargs.get("model", ""))
        progress = get_loading_progress().get(model)
        if progress is not None:
            return (f"⏳ loading {model} into memory — {progress['percent']}% "
                "(responses start once the model is loaded)")
        prefill = get_prefill_progress(model)
        if prefill is None:
            return None
        processed = int(prefill["processed"])
        total = estimate_request_context_tokens(api_kwargs)
        if total and total >= processed:
            return f"⚙ processing prompt — {max(0, min(100, round(processed / total * 100)))}%"
        # Counter past the estimate (estimator undercounted): no honest denominator, label-only.
        return "⚙ processing prompt"
    except Exception:  # noqa: BLE001 — a status nicety must never break a call
        return None


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
        self._hb = threading.Thread(target=self._activity_heartbeat, name="direct-api-activity-hb", daemon=True)
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
        _report_stale_nonstream_kill(self.agent, self.api_kwargs, elapsed, self.stale_timeout, inline=True)
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
                self._abort_client(client, "stale_call_kill", "Inline abort after late client registration failed")
        if stale_before_dispatch:
            raise TimeoutError(
                f"Non-streaming API call timed out before request dispatch (threshold: {int(self.stale_timeout)}s)")
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
        response = _dispatch_nonstreaming_api_request(agent, api_kwargs, make_client=request.make_client)
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
                f"(threshold: {int(stale_timeout)}s)") from None
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
            agent._close_request_openai_client(request_client,
                reason="request_complete" if succeeded else "request_error_cleanup")


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
        return stream if self._stream_close_callable(stream) is None else self.set_client(stream, kind="stream")

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
                abort = (self.agent._abort_request_anthropic_client if request_kind == "anthropic_messages"
                         else self.agent._abort_request_openai_client)
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
    from agent.reasoning_effort import EFFORT_LADDER

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
    codex: bool            # api_mode == codex_responses (codex watchdogs armed)
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
    HERMES_CODEX_TTFB_TIMEOUT_SECONDS,
    HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS (0 disables each),
    HERMES_CODEX_TTFB_DISABLE_ABOVE_TOKENS / HERMES_CODEX_TTFB_STRICT,
    HERMES_CODEX_TTFB_MAX_SECONDS (opt-in ceiling, default 0 = none), HERMES_CODEX_HARD_TIMEOUT_SECONDS.
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
        hard_timeout = env_float("HERMES_CODEX_HARD_TIMEOUT_SECONDS", 1500.0)
        if hard_timeout > 0:
            stale_timeout = min(stale_timeout, hard_timeout)

    idle_default = max(effort_floor, next(
        (default for threshold, default in ((100_000, 180.0), (50_000, 120.0), (10_000, 60.0)) if est_tokens > threshold),
        12.0))

    # No-event TTFB cutoff. Default 120s: the SDK's own read timeout is 600s,
    # and a tight 12s killed subscription-backed requests mid-prefill.
    ttfb_enabled = codex
    ttfb_explicit = env_float("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", -1.0) != -1.0
    ttfb_timeout = env_float("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", 120.0)
    if ttfb_timeout <= 0:
        ttfb_enabled = False
    elif openai_codex_backend:
        # Large requests legitimately spend tens of seconds in admission/prefill before the
        # first SSE event: scale the cutoff up to the idle default unless TTFB_STRICT is set.
        disable_above = env_float("HERMES_CODEX_TTFB_DISABLE_ABOVE_TOKENS", 10_000.0)
        strict = os.environ.get("HERMES_CODEX_TTFB_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}
        if not strict and disable_above > 0 and est_tokens >= disable_above and ttfb_timeout < idle_default:
            logger.info("Scaling openai-codex no-event TTFB watchdog from %.0fs to %.0fs "
                "for large request (context=~%s tokens >= %.0f). "
                "Set HERMES_CODEX_TTFB_STRICT=1 to keep the smaller cutoff.", ttfb_timeout, idle_default,
                f"{est_tokens:,}", disable_above)
            ttfb_timeout = idle_default
        # Opt-in ceiling (0 = off): a 120s default here silently undid the scale-up above (#91621).
        ttfb_cap = env_float("HERMES_CODEX_TTFB_MAX_SECONDS", 0.0)
        if ttfb_cap > 0 and ttfb_timeout > ttfb_cap:
            logger.info("Capping openai-codex no-event TTFB timeout from %.0fs to %.0fs "
                "(context=~%s tokens) per HERMES_CODEX_TTFB_MAX_SECONDS.", ttfb_timeout, ttfb_cap,
                f"{est_tokens:,}")
            ttfb_timeout = ttfb_cap
    elif not ttfb_explicit and (base_url := getattr(agent, "base_url", None)) and is_local_endpoint(base_url):
        # A local server prefills for minutes before its first event; the chat-completions
        # siblings already grant local endpoints the local stale ceiling, so the Responses
        # transport gets the same grace instead of the 120s hosted cutoff (#92302).
        local_ceiling = _local_stream_stale_timeout_default()
        if local_ceiling > ttfb_timeout:
            logger.info("Local provider detected (%s) — no-event TTFB watchdog raised from %.0fs to %.0fs "
                "(agent.local_stream_stale_timeout); set HERMES_CODEX_TTFB_TIMEOUT_SECONDS for an explicit cutoff.",
                base_url, ttfb_timeout, local_ceiling)
            ttfb_timeout = local_ceiling
    if ttfb_enabled and not ttfb_explicit:
        # High-effort thinking precedes the first event; the floor outranks the cap.
        ttfb_timeout = max(ttfb_timeout, effort_floor)

    # An operator-set idle timeout keeps first-event semantics; only the implicit
    # default defers arming until model progress. Sentinel: env_float returns the
    # default for unset AND unparseable values, so both count as implicit.
    idle_explicit = env_float("HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS", -1.0) != -1.0
    idle_timeout = env_float("HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS", idle_default)
    return _NonStreamWatchdogs(stale_timeout=stale_timeout, codex=codex, est_tokens=est_tokens,
        ttfb_enabled=ttfb_enabled, ttfb_timeout=ttfb_timeout, idle_enabled=codex and idle_timeout > 0,
        idle_timeout=idle_timeout,
        idle_requires_progress=(
            codex and openai_codex_backend and codex_floor > 0 and not idle_explicit
        ))


def _codex_silent_hang_hint(agent, api_kwargs: dict) -> Optional[str]:
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
    from agent.chat_completion_nonstream import _NonStreamRequest

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
        from agent.reasoning_params import unset_reasoning_default
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
        if isinstance(cfg, dict) and (
            cfg.get("enabled") is False or cfg.get("effort") == "none"
        ):
            if getattr(agent, "_reasoning_floor_required", False):
                from agent.auxiliary_reasoning_floor import REASONING_FLOOR_EFFORT
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
        from agent.transports.chat_completions import _rename_tool_search_bridge_for_xai
        has_bridge = any(
            (t.get("function") or {}).get("name") == "tool_search" for t in tools_for_api if isinstance(t, dict)
        )
        if has_bridge:
            tools_for_api = _copy_xai.deepcopy(tools_for_api)
            tools_for_api, alias_map = _rename_tool_search_bridge_for_xai(tools_for_api)
            if transport is not None:
                transport._last_wire_aliases = alias_map
    except Exception as exc:
        logger.warning("%s⚠️ Failed to alias tool_search bridge for xAI: %s", getattr(agent, "log_prefix", ""), exc)
    return tools_for_api


def _consume_ephemeral_max_output(agent):
    """Pop the one-shot ephemeral output cap; whichever path builds the request consumes it."""
    ephemeral_out = getattr(agent, "_ephemeral_max_output_tokens", None)
    if ephemeral_out is not None:
        agent._ephemeral_max_output_tokens = None
    return ephemeral_out









def _build_chat_completions_kwargs(agent, api_messages, tools_for_api, reasoning_config, request_overrides, cache_scope_id):
    transport = agent._get_transport()
    tools_for_api = _alias_tool_search_bridge_for_xai(agent, transport, tools_for_api)

    _is_qwen = agent._is_qwen_portal()
    _is_or = agent._is_openrouter_url()
    _host = agent._base_url_lower
    _is_gh = base_url_host_matches(_host, "models.github.ai") or base_url_host_matches(_host, "githubcopilot.com")
    _is_lmstudio = (agent.provider or "").strip().lower() == "lmstudio"

    # _fixed_temperature_for_model may return the OMIT_TEMPERATURE sentinel
    # (temperature omitted entirely), a numeric override, or None.
    _omit_temp, _fixed_temp = False, None
    with contextlib.suppress(Exception):
        from agent.auxiliary_client import _fixed_temperature_for_model, OMIT_TEMPERATURE
        _ft = _fixed_temperature_for_model(agent.model, agent.base_url)
        _omit_temp = _ft is OMIT_TEMPERATURE
        _fixed_temp = None if _omit_temp else _ft

    _prefs = _provider_preferences_for_agent(agent)

    _qwen_meta = {"sessionId": agent.session_id or "hermes", "promptId": str(uuid.uuid4())} if _is_qwen else None
    _profile = None
    with contextlib.suppress(Exception):
        from providers import get_provider_profile
        _profile = get_provider_profile(agent.provider)

    _ephemeral_out = _consume_ephemeral_max_output(agent)
    # Strip image parts for non-vision models on BOTH paths (registered
    # providers with profiles used to bypass it).
    _common = dict(model=agent.model, messages=agent._prepare_messages_for_non_vision_model(api_messages),
        tools=tools_for_api, base_url=agent.base_url, timeout=agent._resolved_api_call_timeout(),
        max_tokens=agent.max_tokens, ephemeral_max_output_tokens=_ephemeral_out,
        max_tokens_param_fn=agent._max_tokens_param, reasoning_config=reasoning_config,
        request_overrides=request_overrides, session_id=getattr(agent, "session_id", None),
        cache_scope_id=cache_scope_id, ollama_num_ctx=agent._ollama_num_ctx,
        provider_preferences=_prefs or None, openrouter_min_coding_score=agent.openrouter_min_coding_score,
        supports_reasoning=agent._supports_reasoning_extra_body(),
        qwen_session_metadata=_qwen_meta)
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
        is_kimi=any(base_url_host_matches(agent.base_url, h) for h in ("api.kimi.com", "moonshot.ai", "moonshot.cn")),
        is_tokenhub=base_url_host_matches(_host, "tokenhub.tencentmaas.com"),
        is_lmstudio=_is_lmstudio,
        is_custom_provider=agent.provider == "custom",
        qwen_prepare_fn=agent._qwen_prepare_chat_messages if _is_qwen else None,
        qwen_prepare_inplace_fn=agent._qwen_prepare_chat_messages_inplace if _is_qwen else None,
        fixed_temperature=_fixed_temp,
        omit_temperature=_omit_temp,
        github_reasoning_extra=agent._github_models_reasoning_extra_body() if _is_gh else None,
        lmstudio_reasoning_options=agent._lmstudio_reasoning_options_cached() if _is_lmstudio else None,
        provider_name=agent.provider,
    )


def build_api_kwargs(agent, api_messages: list, tools_for_api: list | None = None) -> dict:
    """Build the keyword arguments dict for the active API mode.

    Wraps the per-api_mode builder so the conversation-affinity headers (OpenCode's
    ``x-opencode-session``, a custom provider's opt-in ``session_affinity_header``) ride on
    every request regardless of transport (chat_completions / codex_responses /
    anthropic_messages). No-op for every other provider.
    """
    from agent.opencode_affinity import merge_session_affinity_headers

    kwargs = _build_api_kwargs_for_mode(agent, api_messages, tools_for_api)
    return merge_session_affinity_headers(
        kwargs,
        getattr(agent, "provider", None),
        getattr(agent, "base_url", None),
        getattr(agent, "session_id", None),
    )


def _build_api_kwargs_for_mode(agent, api_messages: list, tools_for_api: list | None = None) -> dict:
    # One-shot continuation override — consumed exactly once, on the FIRST
    # request this call builds (only one api_mode branch runs per invocation).
    reasoning_config = _reasoning_config_for_wire(agent)
    if tools_for_api is None:
        tools_for_api = agent.tools
    # The one place request_overrides are consumed: static /fast values are already pinned
    # in agent.request_overrides; auto/cold windows layer the fast override per request.
    request_overrides = effective_request_overrides(agent)
    cache_scope_id = _prompt_cache_scope_for_agent(agent)
    return _build_chat_completions_kwargs(
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


def _assistant_reasoning_text(agent, assistant_message) -> Optional[str]:
    """Structured reasoning, else inline ``<think>`` blocks embedded in content."""
    reasoning_text = agent._extract_reasoning(assistant_message)
    if not reasoning_text:
        content = flatten_message_text(getattr(assistant_message, "content", None))
        think_blocks = re.findall(r'<think>(.*?)</think>', content, flags=re.DOTALL)
        if think_blocks:
            reasoning_text = "\n\n".join(b.strip() for b in think_blocks if b.strip()) or None
    if reasoning_text and agent.verbose_logging:
        logging.debug(f"Captured reasoning ({len(reasoning_text)} chars): {reasoning_text}")
    # When streaming is active the reasoning was already displayed during the
    # stream (structured deltas or <think> tag extraction); fire only for
    # non-streaming modes (gateway, batch, quiet). Anything not shown during
    # streaming is caught by the CLI post-response fallback.
    if reasoning_text and agent.reasoning_callback and not agent.stream_delta_callback and not agent._stream_callback:
        with contextlib.suppress(Exception):
            agent.reasoning_callback(reasoning_text)
    return _sanitize_surrogates(reasoning_text) if reasoning_text else reasoning_text


def _assistant_content_for_storage(agent, assistant_message):
    # Sanitize surrogates (Kimi/GLM via Ollama emit code points that crash json.dumps),
    # strip inline <think> tags at the storage boundary (they leaked to platforms and
    # polluted titles), then redact inlined credentials before the message enters
    # history / state.db / gateway delivery (no-op with HERMES_REDACT_SECRETS off).
    content = _sanitize_surrogates(flatten_message_text(getattr(assistant_message, "content", None)))
    if isinstance(content, str) and content:
        content = agent._strip_think_blocks(content).strip()
        if content:
            from agent.redact import redact_sensitive_text
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
            call_id = agent._deterministic_call_id(getattr(_fn, "name", "") if _fn else "",
                getattr(_fn, "arguments", "{}") if _fn else "{}", index)
    call_id = call_id.strip()

    response_item_id = getattr(tool_call, "response_item_id", None)
    if not isinstance(response_item_id, str) or not response_item_id.strip():
        _, response_item_id = agent._split_responses_tool_id(raw_id)
    response_item_id = agent._derive_responses_function_call_id(call_id,
        response_item_id if isinstance(response_item_id, str) else None)
    # Arguments are deliberately NOT redacted: this dict is replayed to the model every
    # turn, so a ``***`` mask would break credential-dependent commands (#43083).
    tc_dict = {"id": call_id, "call_id": call_id, "response_item_id": response_item_id,
        "type": tool_call.type,
        "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments}}
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
    msg = stamp_message_timestamp({"role": "assistant",
        "content": _assistant_content_for_storage(agent, assistant_message), "reasoning": reasoning_text,
        "finish_reason": finish_reason})

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
    for attr in ("anthropic_content_blocks", "bedrock_content_blocks", "codex_reasoning_items", "codex_message_items"):
        value = getattr(assistant_message, attr, None)
        if value:
            msg[attr] = value
            if attr == "codex_reasoning_items":
                from agent.codex_responses_adapter import (
                    has_replayable_native_compaction_checkpoint,
                )

                note_checkpoint = getattr(
                    agent.context_compressor, "note_native_compaction_checkpoint", None
                )
                if (
                    callable(note_checkpoint)
                    and has_replayable_native_compaction_checkpoint(agent, [msg])
                ):
                    note_checkpoint()
                    # The response priced the pre-checkpoint input, not the next
                    # compacted request. A matching durable prefix is now stale.
                    from agent.usage_anchor import set_usage_anchor

                    set_usage_anchor(agent, None)

    if assistant_tool_calls:
        msg["tool_calls"] = [_assistant_tool_call_dict(agent, tc, i) for i, tc in enumerate(assistant_tool_calls)]
    return msg







































# Keys outside the Chat Completions schema that strict gateways (Fireworks-backed OpenCode
# Go, Mistral, Moonshot/Kimi) reject with 422. The transport's convert_messages() drops them
# in the main loop; the summary path calls chat.completions.create() directly, so mirror it.
_SUMMARY_FOREIGN_MESSAGE_KEYS = ("reasoning", "finish_reason", "tool_name", "codex_reasoning_items",
    "codex_message_items", "timestamp", "platform_message_id")
_EMPTY_SUMMARY_RESPONSE = "I reached the iteration limit and couldn't generate a summary."


















def _build_partial_stream_stub(role, full_content, full_reasoning, model_name, usage_obj, *,
    dropped_tool_names=None, overflow_terminal=False):
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
        choices=[SimpleNamespace(
            index=0,
            message=SimpleNamespace(role=role, content=full_content, tool_calls=None,
                reasoning_content=full_reasoning),
            finish_reason=FINISH_REASON_LENGTH,
        )],
        usage=usage_obj,
        _dropped_tool_names=dropped_tool_names or None,
        _overflow_terminal=overflow_terminal,
    )


# SSE error events from proxies (OpenRouter's {"error":{"message":"Network
# connection lost."}}) surface as SDK APIError without a status_code (unlike
# APIStatusError). They mean the upstream stream died: retry with a fresh
# connection like an httpx drop.
_SSE_CONN_PHRASES = ("connection lost", "connection reset", "connection closed", "connection terminated",
    "network error", "network connection", "terminated", "peer closed", "broken pipe",
    "upstream connect error")










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
            return "".join(t for t in (getattr(part, "text", None) for part in content) if isinstance(t, str))
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






class _ToolCallAccumulator:
    """Assemble streamed tool-call deltas into complete ``tool_calls`` entries
    (``acc``: slot index -> entry dict). Ollama-compatible endpoints reuse index 0
    for every call in a parallel batch, distinguishing them only by id, so a new
    id at an already-seen raw index is redirected to a fresh slot."""

    def __init__(self):
        self.acc: dict = {}
        self._notified: set = set()
        self._last_id_at_idx: dict = {}      # raw_index -> last seen non-empty id
        self._active_slot_by_idx: dict = {}  # raw_index -> current slot in acc
        # Argument deltas are collected per slot and joined once in ``materialize`` —
        # ``+=`` per chunk rebuilds the whole string every delta (quadratic on big args).
        self._argument_parts: dict[int, list[str]] = {}

    def materialize(self) -> dict:
        """Join buffered argument deltas into each entry's ``arguments``; idempotent. Returns ``acc``."""
        for idx, parts in self._argument_parts.items():
            self.acc[idx]["function"]["arguments"] = "".join(parts)
        return self.acc

    def feed(self, tc_delta) -> Optional[str]:
        """Merge one delta; return the tool name the first time it is complete."""
        raw_idx = getattr(tc_delta, "index", None)
        if raw_idx is None:
            raw_idx = 0
        tc_id = getattr(tc_delta, "id", None)
        delta_id = tc_id or ""
        if isinstance(tc_id, int):  # Poolside sends integer ids
            tc_id = str(tc_id)

        self._active_slot_by_idx.setdefault(raw_idx, raw_idx)
        if delta_id and raw_idx in self._last_id_at_idx and delta_id != self._last_id_at_idx[raw_idx]:
            self._active_slot_by_idx[raw_idx] = max(self.acc, default=-1) + 1
        if delta_id:
            self._last_id_at_idx[raw_idx] = delta_id
        idx = self._active_slot_by_idx[raw_idx]

        entry = self.acc.setdefault(
            idx, {"id": tc_id or "", "type": "function", "function": {"name": "", "arguments": ""}, "extra_content": None},
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
            extra = (tc_delta.model_extra if isinstance(tc_delta.model_extra, dict) else {}).get("extra_content")
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
        self.stream_attempt_state = {"current": 0, "cancelled": set(), "discarded_chunks": 0, "discarded_bytes": 0}
        self.managed_stream_holder = {"stream": None}
        # Per-attempt: single-writer token, request-local client, raw HTTP response (chat wire).
        self._writer_token = self._attempt_request_client = self._attempt_stream_response = None
        # The route ``api_kwargs`` was assembled for; a retry must not replay it on another one.
        self._request_route = self._live_route()

    # ── shared small helpers ────────────────────────────────────────────

    def _live_route(self) -> tuple:
        agent = self.agent
        return tuple(str(getattr(agent, attr, "") or "") for attr in ("model", "provider", "base_url", "api_mode"))


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
            return stream_attempt_id == int(state["current"]) and stream_attempt_id not in state["cancelled"]

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
            ("Discarding chunk from superseded stream attempt %s " if first else "Discarded stale stream chunk from attempt %s ")
            + "(discarded_chunks=%s discarded_bytes=%s)",
            stream_attempt_id, discarded_chunks, discarded_bytes,
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
            self._quiet(lambda: (self.agent.stream_delta_callback(text), self.agent._record_streamed_assistant_text(text)))

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
        ``request_timeout_seconds`` wins over HERMES_API_TIMEOUT (1800s) and
        HERMES_STREAM_READ_TIMEOUT (120s); connect/pool cover the handshake, not
        inference: 30s, or capped at 60s when configured."""
        cfg = get_provider_request_timeout(self.agent.provider, self.agent.model)
        base = cfg if cfg is not None else env_float("HERMES_API_TIMEOUT", 1800.0)
        if cfg is not None:
            return base, cfg, min(base, 60.0)
        read = env_float("HERMES_STREAM_READ_TIMEOUT", 120.0)
        stale = self._stream_stale_timeout
        if read == 120.0 and self.agent.base_url and is_local_endpoint(self.agent.base_url):
            read = base  # local providers prefill for minutes
            logger.debug("Local provider detected (%s) — stream read timeout raised to %.0fs", self.agent.base_url, read)
        elif read == 120.0 and stale is not None and stale != float("inf") and stale > read:
            # Reasoning models pause mid-stream for minutes; the stale detector
            # tolerates that, so the raw read timeout must not fire first.
            read = stale
            logger.debug("Cloud reasoning stream — read timeout raised to %.0fs to match stale-stream detector", read)
        return base, read, 30.0

    @staticmethod
    def _choiceless_chunk(chunk, finish_reason):
        """Chunk with empty ``choices`` -> ``(usage, finish_reason)``. Raises
        ProviderStreamError for providers (DeepInfra) that send validation errors
        as in-stream chunks (choices=None + error_type/error_message), which
        would otherwise surface as a misleading EmptyStreamError plus retries."""
        usage = chunk.usage if hasattr(chunk, "usage") and chunk.usage else None  # final usage chunk
        # Without this check the error is silently dropped and the stream ends empty → EmptyStreamError →
        # misleading "empty stream" message and pointless retries on the same bad request. (#65631)
        _err_type = getattr(chunk, "error_type", None)
        _err_msg = getattr(chunk, "error_message", None)
        if _err_type or _err_msg:
            _status = _status_code_from_payload({"code": _err_type, "message": _err_msg}) or _status_code_from_value(_err_type)
            body = _provider_error_body(
                {"code": _err_type or "provider_in_stream_error", "message": str(_err_msg or chunk)}, _status)
            raise ProviderStreamError(status_code=_status, body=body, raw_text=f"{_err_type}: {_err_msg}")
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
        if not is_native_gemini_base_url(self.agent.base_url) and not getattr(self.agent, "_stream_options_unsupported", False):
            stream_kwargs["stream_options"] = {"include_usage": True}
        request_client = self._attempt_request_client = self.clients.set_client(
            self.agent._create_request_openai_client(reason="chat_completion_stream_request", api_kwargs=stream_kwargs))
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
            cancelled = self._request_cancelled["value"] or current in self.stream_attempt_state["cancelled"]
        if not cancelled:
            return
        self._shutdown_stale_attempt_socket(response)
        if self._attempt_request_client is not None:
            # Kind-aware: the anthropic_messages wire (incl. anthropic-compatible custom endpoints)
            # runs on a request-local Anthropic client with its own slot sweep.
            abort = (self.agent._abort_request_anthropic_client if self.agent.api_mode == "anthropic_messages"
                     else self.agent._abort_request_openai_client)
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
                getattr(delta, attr, None) for attr in ("content", "tool_calls", "reasoning_content", "reasoning")):
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
            "single-writer invariant (model=%s).", label, self.api_kwargs.get("model", "unknown"))
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
        response_id = upstream_provider = None  # the provider's own id / serving upstream, from the chunks
        role = "assistant"
        _diag = self._new_diag()
        self._writer_token = self._attempt_request_client = self._attempt_stream_response = None
        def _open_stream(next_api_kwargs: dict[str, Any]):
            timeout = _httpx.Timeout(connect=conn_cap, read=read_timeout, write=base_timeout, pool=conn_cap)
            return self._open_chat_stream({**next_api_kwargs, "stream": True, "timeout": timeout})

        def _flush_pending_stream_text():
            pending_parts = list(pending_text_parts)
            pending_text_parts.clear()
            for text in pending_parts:
                (self._route_suppressed_text if tool_calls_acc else self._emit_text)(text)

        stream = self._set_managed_stream(_open_stream(self.api_kwargs))
        self._chat_stream_created(stream)
        if self.agent.provider == "moa":
            # Hermes interrupts the managed stream; Relay alone closes the provider stream.
            self.clients.set_stream_handle(stream)

        for chunk in _iter_provider_stream_chunks(stream, response=lambda: self._attempt_stream_response):
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
                            self._attempt_request_client, reason="interrupt_stream_close_failed")
                break
            if not self._stream_attempt_is_active(stream_attempt_id):
                self._discard_stale_stream_chunk(stream_attempt_id, chunk)
                continue
            if hasattr(chunk, "model") and chunk.model:
                model_name = chunk.model
            if response_id is None and isinstance(getattr(chunk, "id", None), str) and chunk.id:
                response_id = chunk.id
            if upstream_provider is None and isinstance(getattr(chunk, "provider", None), str) and chunk.provider:
                upstream_provider = chunk.provider  # OpenRouter stamps who served
            if not chunk.choices:
                usage, finish_reason = self._choiceless_chunk(chunk, finish_reason)
                usage_obj = usage or usage_obj
                continue

            choice = chunk.choices[0]
            delta = choice.delta
            # Read finish_reason/usage BEFORE any content-shape `continue`: the SSE-echo
            # guard can swallow a merged finish chunk (vLLM standalone ':' tokens).
            finish_reason = _normalize_finish_reason(getattr(choice, "finish_reason", None)) or finish_reason
            if hasattr(chunk, "usage") and chunk.usage:
                usage_obj = chunk.usage

            reasoning_text = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            # Same ``model_extra`` fallback as the non-streaming path: a reasoning-only stream
            # whose deltas carry only this field otherwise trips the empty-stream guard (#56516).
            if reasoning_text is None and isinstance(getattr(delta, "model_extra", None), dict):
                reasoning_text = delta.model_extra.get("reasoning_content") or delta.model_extra.get("reasoning")
            if reasoning_text:
                # Summary-part models omit the separator between markdown blocks; re-insert it.
                reasoning_text = separate_glued_reasoning_blocks(
                    reasoning_parts[-1] if reasoning_parts else "", reasoning_text)
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
                elif (pending_text_parts or _provider_stream_text_may_be_sse(delta_content)
                        # A shim cannot follow text already released to the display, so the
                        # whole-content re-join runs only until the first emitted delta.
                        or (not self.deltas_were_sent["yes"] and router_timeout_shim_may_follow("".join(content_parts)))):
                    pending_text_parts.append(delta_content)
                    pending = "".join(pending_text_parts)
                    if not (_provider_stream_text_may_be_sse(pending) or router_timeout_shim_may_follow(pending)):
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
        if getattr(stream, "final_response", None) is not None:
            return self._adopt_final_response(stream.final_response)
        return self._finish_chat_stream(stream, role, content_parts, reasoning_parts, tool_calls_acc,
            finish_reason, model_name, usage_obj, flush_pending=_flush_pending_stream_text,
            response_id=response_id, upstream_provider=upstream_provider, reasoning_details=reasoning_details,
            refusal_parts=refusal_parts)

    def _adopt_final_response(self, final_response):
        """Adapter returned a completed response for ``stream=True``: switch the
        session to non-streaming and replay its content as deltas."""
        logger.info("Streaming request returned a final response object instead of an iterator; "
            "switching %s/%s to non-streaming for this session.", self.agent.provider or "unknown",
            self.agent.model or "unknown")
        self.agent._disable_streaming = True
        choices = final_response.choices
        message = getattr(choices[0] if isinstance(choices, (list, tuple)) and choices else None, "message", None)
        if message is not None:
            reasoning_text = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None)
            if reasoning_text is None and isinstance(getattr(message, "model_extra", None), dict):
                reasoning_text = message.model_extra.get("reasoning_content") or message.model_extra.get("reasoning")
            if isinstance(reasoning_text, str) and reasoning_text:
                self._emit_reasoning(reasoning_text)
            content = getattr(message, "content", None)
            if isinstance(content, str) and content:
                self._fire_first_delta()
                self.agent._fire_stream_delta(content)  # not _emit_text: deltas_were_sent stays False here
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
            mock_tool_calls.append(SimpleNamespace(
                id=tc["id"], type=tc["type"], extra_content=tc.get("extra_content"),
                function=SimpleNamespace(name=tc["function"]["name"], arguments=arguments)))
        return mock_tool_calls or None, has_truncated_tool_args

    def _finish_chat_stream(self, stream, role, content_parts, reasoning_parts, tool_calls_acc, finish_reason,
        model_name, usage_obj, *, flush_pending, response_id=None, upstream_provider=None, reasoning_details=None,
        refusal_parts=None):
        """Assemble the non-streaming-shaped response after the chunk loop. A
        stream ending with no finish_reason is a drop, not a completion: return a
        partial-stream stub so the loop fails fast instead of executing empty
        args or stamping "stop"."""
        full_content = "".join(content_parts) or None
        full_reasoning = "".join(reasoning_parts) or None
        mock_tool_calls, has_truncated_tool_args = self._assemble_tool_calls(tool_calls_acc, finish_reason)
        # Zero-chunk guard: nothing usable = upstream error / malformed SSE.
        if finish_reason is None and not content_parts and not reasoning_parts and not refusal_parts and not tool_calls_acc:
            raise EmptyStreamError(
                "Provider returned an empty stream with no finish_reason (possible upstream error or malformed SSE response).")
        if has_truncated_tool_args and finish_reason is None:
            # Partial args WITH finish_reason="length" is a real output cap; with NONE the
            # upstream dropped mid tool-call, and stamping "length" burns 3 useless retries.
            _dropped_names = [(tool_calls_acc[idx]["function"]["name"] or "?") for idx in sorted(tool_calls_acc)]
            logger.warning(
                "Stream ended with no finish_reason while a tool call's arguments were still incomplete "
                "(tools=%s); treating as a mid-tool-call stream drop, not an output-length truncation.",
                _dropped_names)
            return _build_partial_stream_stub(
                role, full_content, full_reasoning, model_name, usage_obj, dropped_tool_names=_dropped_names or None)
        if finish_reason is None and (content_parts or reasoning_parts) and not tool_calls_acc and usage_obj is None:
            # Text-only (or reasoning-only) drop: otherwise the partial text is stamped "stop"
            # and the next step is lost — for reasoning-only, the clean-stop promotion in
            # finish_text_response would then surface a truncated thought as the answer.
            # A usage object proves the provider finished (include_usage's final chunk).
            logger.warning(
                "Stream ended with no finish_reason after delivering text with no tool calls; treating as a mid-stream drop.")
            return _build_partial_stream_stub(role, full_content, full_reasoning, model_name, usage_obj)
        effective_finish_reason = "length" if has_truncated_tool_args else (finish_reason or "stop")
        provider_stream_error = _provider_stream_error_from_text(
            full_content or "", effective_finish_reason, response=getattr(stream, "response", None))
        if provider_stream_error is not None:
            raise provider_stream_error
        message = SimpleNamespace(role=role, content=full_content, tool_calls=mock_tool_calls, reasoning_content=full_reasoning,
            # ``normalize_response`` reads ``message.refusal`` — same contract as the non-streaming object.
            refusal="".join(refusal_parts or ()) or None)
        if reasoning_details:
            # Only when present: _build_assistant_message's passthrough persists them
            # for replay, and non-reasoning providers keep the attribute absent.
            message.reasoning_details = reasoning_details
        # The provider's id when the chunks carried one (chatcmpl-/gen-...): it is what a provider needs to
        # look a request up. Fabricated only when the stream never sent one.
        response = SimpleNamespace(id=response_id or ("stream-" + str(uuid.uuid4())), model=model_name, usage=usage_obj,
            provider=upstream_provider,
            choices=[SimpleNamespace(index=0, message=message, finish_reason=effective_finish_reason)])
        # A held router timeout shim (#68396) is rejected by validate_response and retried;
        # releasing its text here would show the provider failure as assistant output.
        if not is_router_timeout_shim(response):
            flush_pending()
        return response

    # ── anthropic_messages wire ─────────────────────────────────────────



    # ── retry loop ──────────────────────────────────────────────────────




    def _call_wire(self, stream_attempt_id: int):
        return self._call_chat_completions(stream_attempt_id)

    def _call(self):
        stream_attempt_id = self._start_stream_attempt()
        try:
            if self.agent._interrupt_requested:
                self._cancel_current_stream_attempt("interrupt_before_stream")
                raise InterruptedError("Agent interrupted before stream")
            self.result["response"] = _with_stream_emitters(
                self.agent, lambda: self._call_wire(stream_attempt_id))
        except Exception as e:
            self.result["error"] = e
        finally:
            self._close_managed_stream()
            self.clients.close_once(
                "stream_request_complete" if self.result["response"] is not None
                else "stream_error_cleanup")


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
            from agent.agent_runtime_helpers import (
                _connection_candidates, _shutdown_socket, _socket_from_candidate,
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
                    logger.info("Shut down the stale stream's socket to unblock the reader "
                                "(attempt superseded; model=%s).", self.api_kwargs.get("model", "unknown"))
                    return
            logger.debug("Stale stream socket shutdown found no socket; pool sweep is the only abort")
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
            elapsed, self._stream_stale_timeout, self.api_kwargs.get("model", "unknown"), f"{_est_ctx:,}",
        )
        self.agent._buffer_diagnostic_status(
            f"⚠️ No response from provider for {int(elapsed)}s (model: {self.api_kwargs.get('model', 'unknown')}, "
            f"context: ~{_est_ctx:,} tokens). Reconnecting...")
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
        self.agent._emit_diagnostic_wait(f"⚠ no output from provider for {int(elapsed)}s — reconnecting...")
        self.agent._touch_activity(f"stale stream detected after {int(elapsed)}s, reconnecting")

    def _abort_for_interrupt(self, stale_elapsed: float) -> None:
        """/stop seen by the monitor: mark cancelled, abort the request-local
        socket, wait for the worker, flag the interrupt."""
        # The stale branch already counted this iteration if its deadline won the race.
        if stale_elapsed <= self._stream_stale_timeout:
            _record_interrupted_provider_wait(self.agent, stale_elapsed, response_started=self.deltas_were_sent["yes"])
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
        HERMES_LOCAL_STREAM_STALE_TIMEOUT — an infinite one stalled sessions on a
        crashed endpoint forever. Cloud values scale with context size and are
        floored for known reasoning models (else BrokenPipeError from the gateway)."""
        base = _configured_stale_base(self.agent)
        if base == 180.0 and self.agent.base_url and is_local_endpoint(self.agent.base_url):
            self._stream_stale_timeout = _local_stream_stale_timeout_default()
            logger.debug("Local provider detected (%s) — stale stream timeout set to %.0fs",
                self.agent.base_url, self._stream_stale_timeout)
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
        _partial_text = (getattr(self.agent, "_current_streamed_assistant_text", "") or "").strip() or None
        _partial_names = list(self.result.get("partial_tool_names") or [])
        if _partial_names:
            # User-visible warning so the user and model both know what was attempted.
            _name_str = ", ".join(_partial_names[:3])
            if len(_partial_names) > 3:
                _name_str += f", +{len(_partial_names) - 3} more"
            _warn = (f"\n\n⚠ Stream stalled mid tool-call ({_name_str}); the action was not executed. "
                     f"Ask me to retry if you want to continue.")
            _partial_text = (_partial_text or "") + _warn  # model/result bookkeeping, never gated
            if self.agent._warning_presentation_enabled():
                self._quiet(self.agent._fire_stream_delta, _warn)  # visible immediately
            logger.warning(
                "Partial stream dropped tool call(s) %s after %s chars of text; surfaced warning to user: %s",
                _partial_names, len(_partial_text or ""), error)
        _reset_stale_streak(self.agent)
        if not _partial_names:
            logger.warning(
                "Partial stream delivered before error; returning length-truncated stub with %s chars of "
                "recovered content so the loop can continue from where the stream died: %s",
                len(_partial_text or ""), error)
        return _build_partial_stream_stub(
            "assistant", _partial_text, None, getattr(self.agent, "model", "unknown"), None,
            dropped_tool_names=_partial_names)


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
                target=_context_thread_target(self._monitor_loop), name="stream-inline-monitor", daemon=True)
            monitor.start()
            try:
                self._run_call()
            finally:
                monitor.join(timeout=2.0)
        else:
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
        if self.result["response"] is not None:
            _reset_stale_streak(self.agent)  # provider proved responsive: clear the breaker
        # Propagate first-chunk timing for the ``post_api_request`` hook.
        if isinstance(self.clients.diag, dict) and self.clients.diag.get("first_chunk_at"):
            self.agent._last_api_first_chunk_at = float(self.clients.diag["first_chunk_at"])
        return self.result["response"]


def try_activate_fallback(agent, reason=None) -> bool:
    return False


def interruptible_streaming_api_call(agent, api_kwargs: dict, *, on_first_delta=None):
    """Streaming variant of _interruptible_api_call: fires the delta callbacks per
    text token (tool-call turns suppress them) and returns a SimpleNamespace in
    the non-streaming response shape. codex_responses delegates to the already-
    streaming codex runner; cron turns and delegated children run inline."""
    if agent._interrupt_requested:
        raise InterruptedError("Agent interrupted before streaming API call")
    # Cross-turn stale-stream circuit breaker (see ``_stale_streak()``).
    _check_stale_giveup(agent)
    return _StreamingCall(agent, api_kwargs, on_first_delta).run()


__all__ = ["interruptible_api_call", "build_api_kwargs", "build_assistant_message", "interruptible_streaming_api_call"]
