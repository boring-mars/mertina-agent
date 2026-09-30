# Ported from hermes-agent agent/turn_recovery.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Recovery-branch handlers for the conversation turn's inner retry loop.

When the model call raises, one-shot recovery chains run before the generic retry/backoff
path. Handlers return ``True`` (request repaired in place; loop ``continue``s with the same
``retry_count``) or ``False`` (fall through). Guards live on ``TurnRetryState``; handlers
mutate ``agent`` / ``messages`` / ``api_messages`` in place. Logger name stays
``agent.conversation_loop`` (caplog pins); that module is only imported lazily (cycle + patch sites).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from mertina.agent.conversation_compression import COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE
from mertina.agent.error_classifier import FailoverReason
from mertina.agent.message_sanitization import (
    close_interrupted_tool_sequence,
)
from mertina.agent.model_metadata import (
    is_output_cap_error,
    parse_available_output_tokens_from_error,
)
from mertina.agent.retry_utils import (
    is_zai_coding_overload_error,
    zai_coding_overload_retry_ceiling,
)
from mertina.agent.thinking_timeout_guidance import (
    build_thinking_timeout_guidance,
    is_thinking_timeout,
)
from mertina.agent.turn_failure_copy import (
    CONTENT_POLICY_NEXT_STEPS,
    content_policy_copy,
    exhausted_copy,
    limit_reset_copy,
    nonretryable_copy,
    provider_label_for,
    site_copy,
    stamp_failure,
)
from mertina.agent.turn_retry_state import TurnRetryState
from mertina.constants import display_mertina_home
from mertina.utils import base_url_host_matches

logger = logging.getLogger("mertina.agent.conversation_loop")


def _vlines(agent: Any, *lines: str) -> None:
    """Force-``_vprint`` each line prefixed with ``agent.log_prefix``."""
    for line in lines:
        agent._vprint(f"{agent.log_prefix}{line}", force=True, diagnostic=True)


def _blines(agent: Any, *lines: str) -> None:
    """``_buffer_vprint`` each line (surfaces only if every retry+fallback exhausts)."""
    for line in lines:
        agent._buffer_vprint(line)


def _failed_turn_result(
    final_response: str, messages: Any, api_call_count: int, error: str
) -> dict[str, Any]:
    """Base failed-turn result dict shared by the two terminal paths."""
    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "failed": True,
        "error": error,
    }


def limit_reset_epoch(agent: Any, api_error: Exception) -> float | None:
    """Epoch seconds when the provider says its limit lifts (Retry-After header, ``resets_at`` /
    ``retry_after`` body fields, "try again in N" text) — the same datum the backoff honours."""
    from mertina.agent.credential_pool import _parse_absolute_timestamp

    try:
        return _parse_absolute_timestamp(
            agent._extract_api_error_context(api_error).get("reset_at")
        )
    except Exception:  # advisory only — never break the error path
        return None


def _stamp_limit_reset(result: dict[str, Any], agent: Any, api_error: Exception) -> None:
    """``failure_resets_at`` for structured clients (Desktop card: "Limit resets at HH:mm") and the
    same sentence appended to the chat text every plain surface (CLI/TUI/gateway) renders (#98852)."""
    resets_at = limit_reset_epoch(agent, api_error)
    if resets_at is None:
        return
    result["failure_resets_at"] = resets_at
    if line := limit_reset_copy(resets_at):
        result["final_response"] = f"{result['final_response']}\n\n{line}"


def _print_nonretryable_auth_guidance(
    agent: Any,
    classified: Any,
    *,
    status_code: int | None,
    provider: Any,
    base_url: Any,
    model: Any,
) -> None:
    """Actionable guidance for a terminal auth / billing error."""
    from mertina.agent.conversation_loop import (
        _print_billing_or_entitlement_guidance,
        _print_nous_entitlement_guidance,
    )

    if classified.reason == FailoverReason.billing and _print_billing_or_entitlement_guidance(
        agent,
        capability="model access",
        provider=provider,
        base_url=str(base_url),
        model=model,
        unverified=classified.billing_unverified,
    ):
        return
    if provider == "nous" and _print_nous_entitlement_guidance(agent, "Nous model access"):
        return
    if provider in {"openai-codex", "xai-oauth", "nous"} and status_code == 401:
        if provider == "openai-codex":
            from mertina.agent.turn_failure_copy import oauth_relogin_command

            _vlines(
                agent,
                "   💡 Codex OAuth token was rejected (HTTP 401). Your token may have been",
                "      refreshed by another client (Codex CLI, VS Code) or another Mertina profile.",
                f"      Sign this profile in again: `{oauth_relogin_command(provider)}`",
            )
        elif provider == "xai-oauth":
            _vlines(
                agent,
                "   💡 xAI OAuth token was rejected (HTTP 401). To fix:",
                "      re-authenticate with xAI Grok OAuth (SuperGrok / Premium+) from `mertina model`.",
            )
        else:  # nous
            _vlines(
                agent,
                "   💡 Nous Portal OAuth token was rejected (HTTP 401). Your token may be",
                "      expired, revoked, or your account may be out of credits. To fix:",
                "      1. Re-authenticate: mertina portal",
                "      2. Check your portal account: https://portal.nousresearch.com",
            )
            # ``:free`` is OpenRouter slug syntax; Nous Portal will reject the model
            # name even after a successful re-auth.
            if isinstance(model, str) and model.endswith(":free"):
                _vlines(
                    agent,
                    f"      ⚠️  Note: `{model}` looks like an OpenRouter slug (`:free` suffix).",
                    "         Nous Portal won't recognize that model name. Either switch to a",
                    f"         Nous catalog model, or run `/model openrouter:{model}` to use OpenRouter.",
                )
        return
    _vlines(
        agent,
        "   💡 Your API key was rejected by the provider. Check:",
        "      • Is the key valid? Run: mertina setup",
        f"      • Does your account have access to {model}?",
    )
    if base_url_host_matches(str(base_url), "openrouter.ai"):
        _vlines(agent, "      • Check credits: https://openrouter.ai/settings/credits")


def _welcome_tier_guidance(classified: Any, *, model: Any, in_chat: bool, door: bool = True) -> str:
    """Copy for a Nous free-tier refusal the classifier parsed (``welcome_refusal`` /
    ``welcome_route`` in ``error_context``); empty for every other error."""
    ctx = getattr(classified, "error_context", None) or {}
    refusal, route = ctx.get("welcome_refusal"), ctx.get("welcome_route")
    if not refusal and not route:
        return ""
    from mertina.cli.anon_auth import welcome_refusal_copy, welcome_route_refusal_copy

    if refusal:
        return welcome_refusal_copy(refusal, model=str(model or ""), in_chat=in_chat, door=door)
    return welcome_route_refusal_copy(str(route), in_chat=in_chat, door=door)


# Closed table: every card kind the desktop has copy for. An unknown gateway reason lands on
# "refused" (generic card, sentence kept) rather than a code the desktop cannot key on.
_WELCOME_SURFACE_KINDS = {
    "rate_limited": "rate_limited",
    "at_capacity": "at_capacity",
    "admission_closed": "at_capacity",
    "model_not_free": "model_not_free",
    "feature_not_free": "model_not_free",
}


def _welcome_surface_kind(classified: Any) -> str:
    """The free-tier failure kind a client renders its card from (``error_surface`` code
    ``free_tier_<kind>``): the welcome refusal's reason, or the route refusal; "" otherwise."""
    ctx = getattr(classified, "error_context", None) or {}
    refusal = ctx.get("welcome_refusal") if isinstance(ctx, dict) else None
    if isinstance(refusal, dict):
        return _WELCOME_SURFACE_KINDS.get(str(refusal.get("reason") or ""), "refused")
    route = ctx.get("welcome_route") if isinstance(ctx, dict) else None
    if route == "tier_disabled":
        return "disabled"
    # A named account on the welcome host has already signed in: no sign-in card, copy only.
    if route == "named_on_welcome_host":
        return ""
    return "route" if route else ""


def _stamp_free_tier(result: dict[str, Any], kind: str, message: str) -> dict[str, Any]:
    """Structured free-tier failure block: ``error_surface`` keys its code on ``kind`` and a client
    shows ``message`` (the chat sentence) as the card body instead of its own generic copy."""
    result["free_tier"] = {"kind": kind or "refused", "message": message}
    return result


def _welcome_outage_copy(base_url: Any, classified: Any, *, anonymous: bool = False) -> str:
    """On the Nous free tier, a transport / server failure that outlived every retry reads as one
    plain sentence (the free model is having trouble) rather than the technical summary. Empty
    for every other route and for rate limits / billing, which have their own copy."""
    try:
        from mertina.cli.anon_auth import FREE_TIER_OUTAGE_COPY, route_is_welcome_host

        # Both: an anonymous JWT sent to a user-overridden paid host never reached the free model.
        if not anonymous or not route_is_welcome_host(base_url):
            return ""
        # Not ``unknown``: that is the classifier's catch-all for status-less local failures, which
        # are not the free model's trouble.
        if classified.reason in (
            FailoverReason.timeout,
            FailoverReason.overloaded,
            FailoverReason.server_error,
        ):
            return FREE_TIER_OUTAGE_COPY
    except Exception:
        pass
    return ""


# Terminal status label per non-retryable reason (default names the HTTP status).
_NONRETRYABLE_LABELS = {
    FailoverReason.content_policy_blocked: "The provider's safety filter refused this request",
    FailoverReason.upstream_blocked: "A firewall/CDN in front of the provider blocked this request",
    FailoverReason.ssl_cert_verification: "The provider's security certificate could not be verified",
    # Only reached after the one-shot image shrink ran (recover_after_classification sets the flag first).
    FailoverReason.image_too_large: "Request still exceeded the provider's size limit after shrinking images",
}


def _missing_vendor_prefix_suggestion(
    api_error: Exception, provider: Any, model: Any
) -> str | None:
    """Prefixed catalogue id when a bare 404 most likely means ``vendor/model`` lost its prefix."""
    if getattr(api_error, "status_code", None) != 404:
        return None
    try:
        from mertina.cli.model_normalize import suggest_prefixed_model_id

        return suggest_prefixed_model_id(str(provider or ""), str(model or ""))
    except Exception:
        return None


def nonretryable_client_error_result(
    agent: Any,
    api_error: Exception,
    classified: Any,
    *,
    status_code: int | None,
    api_kwargs: Any,
    api_messages: Any,
    messages: list[dict[str, Any]],
    conversation_history: Any,
    api_call_count: int,
    approx_tokens: int,
    provider: Any,
    base_url: Any,
    model: Any,
) -> dict[str, Any]:
    """Terminal path for a non-retryable 4xx once fallback is exhausted: debug dump, flush
    the retry trace, print auth / billing / content-policy / TLS guidance, persist (skipped
    for likely context-overflow 400s so the failure does not grow the session), build result."""
    # Result/guidance helpers stay in the loop module (tests import + patch them there).
    from mertina.agent.conversation_loop import (
        _billing_failure_result,
        _content_policy_blocked_result,
    )

    if api_kwargs is not None:
        agent._dump_api_request_debug(
            api_kwargs, reason="non_retryable_client_error", error=api_error
        )
    # Terminal — flush buffered context so the user sees what was tried before the abort.
    agent._flush_status_buffer()
    # Summarize once: Cloudflare/proxy HTML pages and raw provider bodies must be
    # collapsed here or they leak verbatim via the ``error`` field.
    _nonretryable_summary = agent._summarize_api_error(api_error)
    _plabel = provider_label_for(provider)
    _label = _NONRETRYABLE_LABELS.get(
        classified.reason, f"{_plabel} rejected the request and retrying won't help"
    )
    agent._emit_diagnostic_status(f"❌ {_label}: {_nonretryable_summary}")
    # The endpoint/status trace is developer detail: verbose only (the log has it always).
    if getattr(agent, "verbose_logging", False):
        _vlines(
            agent,
            f"   🔌 Provider: {provider}  Model: {model}  (HTTP {status_code})",
            f"   🌐 Endpoint: {base_url}",
        )
    _welcome_hint = _welcome_tier_guidance(classified, model=model, in_chat=False)
    _prefix_suggestion = _missing_vendor_prefix_suggestion(api_error, provider, model)
    if _welcome_hint:
        # A free-tier gate or a wrong-host refusal: the way forward is a sign-in or another
        # provider, never the key/credits advice below.
        _vlines(agent, f"   💡 {_welcome_hint}")
    elif classified.is_auth or classified.reason == FailoverReason.billing:
        _print_nonretryable_auth_guidance(
            agent,
            classified,
            status_code=status_code,
            provider=provider,
            base_url=base_url,
            model=model,
        )
    elif classified.reason == FailoverReason.model_not_found:
        _vlines(
            agent, f"   💡 Model '{model}' isn't available on {_plabel}. Pick another with /model."
        )
        if _prefix_suggestion:
            _vlines(
                agent,
                f"      Did you mean '{_prefix_suggestion}'? It looks like the vendor prefix is missing.",
            )
    elif classified.reason not in _NONRETRYABLE_LABELS:
        _vlines(
            agent,
            f"   💡 Fix: pick another model (/model), or check `{display_mertina_home()}/logs/agent.log`.",
        )
    # A WAF/CDN block (#53099, #70566): the key never reached the provider; the usual cause
    # is the SDK User-Agent, which the per-provider extra_headers override.
    if classified.reason == FailoverReason.upstream_blocked:
        _vlines(
            agent,
            "   💡 The endpoint's firewall/CDN blocked the request before it reached the model — your key",
            "      and model access are probably fine. Relays often reject the SDK's default User-Agent:",
            "      set `extra_headers: {User-Agent: MertinaAgent/1.0}` on the custom_providers entry,",
            "      or check the proxy/WAF rules and your network.",
        )
    # Content-policy blocks: the provider refused this prompt, so recovery is a rephrase
    # or another model, not key/retry advice.
    if classified.reason == FailoverReason.content_policy_blocked:
        _vlines(
            agent,
            f"   💡 {CONTENT_POLICY_NEXT_STEPS}",
            "      To route future blocks to another provider automatically: mertina fallback add",
        )
    # TLS certificate failures are environment problems — name the knobs for each cause.
    if classified.reason == FailoverReason.ssl_cert_verification:
        _vlines(
            agent,
            "   💡 Mertina couldn't verify the provider's security certificate. This fails the same",
            "      way on every retry — fix the environment, then try again:",
            "      • Corporate TLS-inspecting proxy? Point Python at its CA bundle:",
            "        export SSL_CERT_FILE=/path/to/corp-ca.pem  (also REQUESTS_CA_BUNDLE)",
            "      • Missing/stale system CA store? Refresh it (in Mertina's venv: `uv pip install",
            "        --upgrade certifi`; macOS: run 'Install Certificates.command').",
            "      • Self-signed local endpoint (llama.cpp, LM Studio, vLLM)? Use http://",
            "        for localhost, or add the server's cert to your trust store.",
        )
    logger.error("%sNon-retryable client error: %s", agent.log_prefix, api_error)
    # Skip persistence on likely context-overflow (400 + large session): persisting the
    # failed message grows the session and repeats the failure.
    # Persisting the failed user message would make the session even larger, causing the same failure on the
    # next attempt. (#1630)
    if status_code == 400 and (approx_tokens > 50000 or len(api_messages) > 80):
        _vlines(
            agent,
            "⚠️  Skipping session persistence for large failed session to prevent growth loop.",
        )
    else:
        agent._persist_session(messages, conversation_history)
    if classified.reason == FailoverReason.content_policy_blocked:
        return _content_policy_blocked_result(
            messages,
            api_call_count,
            final_response="⚠️ " + content_policy_copy(label=_plabel, summary=_nonretryable_summary),
            error_detail=_nonretryable_summary,
        )
    # Billing walls get the same structured recovery descriptor as the max-retries path
    # so every surface renders one consistent signal.
    if classified.reason == FailoverReason.billing:
        return _billing_failure_result(
            classified=classified,
            summary=_nonretryable_summary,
            messages=messages,
            api_call_count=api_call_count,
            provider=provider,
            base_url=base_url,
            model=model,
        )
    if _welcome_hint:
        # A free-tier refusal is fully explained by its own sentence; the raw provider summary
        # (status codes, JSON) is for the log, not for a first-time user's chat.
        _final_response = _welcome_tier_guidance(classified, model=model, in_chat=True)
    else:
        # Every surface reads final_response; the CLI hint lines above never reach chat.
        _final_response = nonretryable_copy(
            classified,
            provider=provider,
            model=model,
            summary=_nonretryable_summary,
            prefix_suggestion=_prefix_suggestion,
        )
    result = _failed_turn_result(_final_response, messages, api_call_count, _nonretryable_summary)
    # Same verdict fields as the max-retries path: without them the UI descriptor
    # (agent/error_surface.py) reads a rejected OAuth token as a retryable
    # "Provider error" and offers Retry instead of a re-login.
    result.update(
        {
            "failure_reason": classified.reason.value,
            "failure_retryable": bool(classified.retryable),
        }
    )
    _stamp_limit_reset(result, agent, api_error)
    if _welcome_hint and (_kind := _welcome_surface_kind(classified)):
        # The card form: the desktop renders the sign-in as a button, so no "To sign in" tail.
        _stamp_free_tier(
            result, _kind, _welcome_tier_guidance(classified, model=model, in_chat=True, door=False)
        )
    return result


_STREAM_DROP_MARKERS = (
    "connection lost",
    "connection reset",
    "connection closed",
    "network connection",
    "network error",
    "terminated",
)


def max_retries_exhausted_result(
    agent: Any,
    api_error: Exception,
    classified: Any,
    *,
    max_retries: int,
    is_rate_limited: bool,
    error_msg: str,
    api_kwargs: Any,
    api_messages: Any,
    messages: list[dict[str, Any]],
    conversation_history: Any,
    api_call_count: int,
    approx_tokens: int,
    provider: Any,
    base_url: Any,
    model: Any,
) -> dict[str, Any]:
    """Terminal path once retries, transport recovery and fallback all failed: flush the
    trace, emit the billing / rate-limit / generic status, print stream-drop or thinking-timeout
    guidance (the latter wins), persist, build the result with ``failure_reason`` /
    ``failure_retryable`` / ``billing_block``."""
    # Result/guidance helpers stay in the loop module (tests import + patch them there).
    from mertina.agent.conversation_loop import (
        _billing_block_dict,
        _billing_or_entitlement_message,
        _billing_terminal_label,
        _print_billing_or_entitlement_guidance,
    )
    from mertina.cli.anon_auth import is_anonymous_agent

    agent._flush_status_buffer()
    _final_summary = agent._summarize_api_error(api_error)
    _billing_guidance = ""
    _is_billing = classified.reason == FailoverReason.billing
    if _is_billing:
        if classified.billing_unverified:
            # Ambiguous body — hedge the terminal line.
            agent._emit_diagnostic_status(
                "❌ Provider reported usage/credit exhaustion "
                f"(unverified — may be a content-filter rejection) — {_final_summary}"
            )
        else:
            agent._emit_diagnostic_status(f"❌ Billing or credits exhausted — {_final_summary}")
        _billing_kw = dict(
            capability="model access",
            provider=provider,
            base_url=str(base_url),
            model=model,
            unverified=classified.billing_unverified,
        )
        _billing_guidance = _billing_or_entitlement_message(**_billing_kw)
        _print_billing_or_entitlement_guidance(agent, **_billing_kw)
    elif is_rate_limited:
        _reset = reset_hint(api_error)
        agent._emit_diagnostic_status(
            f"❌ Rate limited after {max_retries} retries — {_final_summary}"
            f"{f' (resets in {_reset})' if _reset else ''}"
        )
    else:
        agent._emit_diagnostic_status(
            f"❌ API failed after {max_retries} retries — {_final_summary}"
        )
    _vlines(agent, f"   💀 Final error: {_final_summary}")
    _welcome_hint = _welcome_tier_guidance(classified, model=model, in_chat=False)
    if _welcome_hint:
        _vlines(agent, f"   💡 {_welcome_hint}")

    # SSE stream-drop (e.g. "Network connection lost"): usually a proxy/CDN cutting a very
    # large tool call mid-response.
    _is_stream_drop = not getattr(api_error, "status_code", None) and any(
        p in error_msg for p in _STREAM_DROP_MARKERS
    )
    if _is_stream_drop:
        _vlines(
            agent,
            "   💡 The provider's stream connection keeps dropping. This often happens "
            "when the model tries to write a very large file in a single tool call.",
            "      Try asking the model to use execute_code with Python's open() for "
            "large files, or to write the file in smaller sections.",
        )

    # A known reasoning model hit a transport error before the first content token.
    # Distinct from _is_stream_drop; detection lives in agent.thinking_timeout_guidance.
    _is_thinking_timeout = is_thinking_timeout(classified, model, error_msg)
    if _is_thinking_timeout:
        _vlines(
            agent,
            f"   💡 {build_thinking_timeout_guidance(provider=provider, model=model).strip()}",
        )

    logger.error(
        "%sAPI call failed after %s retries. %s | provider=%s model=%s msgs=%s tokens=~%s",
        agent.log_prefix,
        max_retries,
        _final_summary,
        provider,
        model,
        len(api_messages),
        f"{approx_tokens:,}",
    )
    if api_kwargs is not None:
        agent._dump_api_request_debug(api_kwargs, reason="max_retries_exhausted", error=api_error)
    agent._persist_session(messages, conversation_history)
    _billing_block = None
    _billing_unverified = False
    _free_tier_kind = ""
    if _is_billing:
        _billing_unverified = classified.billing_unverified
        _final_response = _billing_terminal_label(_final_summary, _billing_unverified)
        if _billing_guidance:
            _final_response += f"\n\n{_billing_guidance}"
        # Structured recovery descriptor so every surface renders the same link + label.
        _billing_block = _billing_block_dict(
            provider, base_url, model, _billing_guidance, unverified=_billing_unverified
        )
    else:
        # Every surface reads final_response (the 💡 lines above are CLI-only), so the chat
        # text carries the plain what-happened + next step itself.
        _reset_at = classified.error_context.get("reset_at")
        _final_response = exhausted_copy(
            classified.reason.value,
            label=provider_label_for(provider),
            attempts=max_retries,
            summary=_final_summary,
            reset_seconds=_reset_at - time.time() if _reset_at else None,
        )
        if _welcome_hint:
            _final_response = _welcome_tier_guidance(classified, model=model, in_chat=True)
            _free_tier_kind = _welcome_surface_kind(classified)
        elif _outage := _welcome_outage_copy(
            base_url, classified, anonymous=is_anonymous_agent(agent)
        ):
            _final_response, _free_tier_kind = _outage, "outage"
    if _is_thinking_timeout:
        # Thinking-timeout guidance overrides stream-drop guidance, which would wrongly
        # suggest splitting large file writes.
        _final_response += "\n\n" + build_thinking_timeout_guidance(provider=provider, model=model)
    elif _is_stream_drop:
        _final_response += (
            "\n\nThe connection kept dropping while the model was writing — this often "
            "happens when it writes a very large file in one go. Ask me to write the file in "
            "smaller sections (or via execute_code with Python's open())."
        )
    result = _failed_turn_result(_final_response, messages, api_call_count, _final_summary)
    result.update(
        {
            # Classified reason so callers (kanban worker in cli.py) can tell a quota wall
            # (``rate_limit`` / ``billing``) from a task failure.
            "failure_reason": classified.reason.value,
            # The classifier's own retry verdict — UI surfaces use this, not the reason string.
            "failure_retryable": bool(classified.retryable),
            # True when the billing verdict rests on an ambiguous body.
            "billing_unverified": _billing_unverified,
            # Present only for billing walls: (provider, billing_url, is_nous, message).
            "billing_block": _billing_block,
        }
    )
    _stamp_limit_reset(result, agent, api_error)
    if _free_tier_kind:
        _stamp_free_tier(
            result,
            _free_tier_kind,
            (
                _welcome_tier_guidance(classified, model=model, in_chat=True, door=False)
                if _welcome_hint
                else _final_response
            ),
        )
    return result


def log_api_error_attempt(
    agent: Any,
    api_error: Exception,
    *,
    retry_count: int,
    max_retries: int,
    status_code: int | None,
    elapsed_time: float,
    api_messages: Any,
    approx_tokens: int,
    retryable: bool = True,
) -> tuple[str, str, Any, Any, Any]:
    """Log one failed API attempt (warning + buffered retry trace, OpenRouter "no tool
    endpoints" hint, bare-404 missing-vendor-prefix hint); the buffer only surfaces if every
    retry+fallback exhausts. Returns ``(error_type, error_msg, provider, base_url, model)``.

    ``retryable=False`` (the classifier's verdict, e.g. a 401 on a static-key route) is
    named on the line: a bare ``attempt 1/3`` promises a second attempt that never comes
    and sends readers hunting for a retry bug (#73237)."""
    error_type = type(api_error).__name__
    error_msg = str(api_error).lower()
    _error_summary = agent._summarize_api_error(api_error)
    _attempt = f"attempt {retry_count}/{max_retries}" + ("" if retryable else ", not retryable")
    logger.warning(
        "API call failed (%s) error_type=%s %s summary=%s",
        _attempt,
        error_type,
        agent._client_log_context(),
        _error_summary,
    )

    _provider = getattr(agent, "provider", "unknown")
    _base = getattr(agent, "base_url", "unknown")
    _model = getattr(agent, "model", "unknown")
    _blines(agent, f"⚠️  {_attempt[0].upper()}{_attempt[1:]} failed: {_error_summary}")
    # Exception class, endpoint, raw body and token counts are developer detail: verbose only.
    if getattr(agent, "verbose_logging", False):
        _status_code_str = f" [HTTP {status_code}]" if status_code else ""
        _blines(
            agent,
            f"   🔌 {error_type}{_status_code_str}  Provider: {_provider}  Model: {_model}",
            f"   🌐 Endpoint: {_base}",
        )
        if status_code and status_code < 500:
            _err_body = getattr(api_error, "body", None)
            _err_body_str = str(_err_body)[:300] if _err_body else None
            if _err_body_str:
                _blines(agent, f"   📋 Details: {_err_body_str}")
        _blines(
            agent,
            f"   ⏱️  Elapsed: {elapsed_time:.2f}s  Context: {len(api_messages)} msgs, ~{approx_tokens:,} tokens",
        )

    if agent._is_openrouter_url() and "support tool use" in error_msg:
        _blines(
            agent,
            f"   💡 No OpenRouter providers for {_model} support tool calling with your current settings.",
        )
        from mertina.agent.chat_completion_helpers import _provider_preferences_for_agent

        if _provider_preferences_for_agent(agent).get("only"):
            _blines(
                agent,
                "      Your provider_routing.only restriction is filtering out tool-capable providers.",
                "      Try removing the restriction or adding providers that support tools for this model.",
            )
        _blines(
            agent,
            f"      Check which providers support tools: https://openrouter.ai/models/{_model}",
        )

    # Bare 404 on a ``vendor/model`` catalogue usually means the id lost its prefix; the
    # provider never names the model, so we do.
    _suggestion = _missing_vendor_prefix_suggestion(api_error, _provider, _model)
    if _suggestion:
        _blines(
            agent,
            f"   💡 Model '{_model}' is not a valid id for provider {_provider} — it is missing its vendor prefix.",
            f"      Did you mean '{_suggestion}'?  Re-pick it with /model.",
        )
    return error_type, error_msg, _provider, _base, _model


def abort_turn_on_interrupt(
    agent: Any,
    messages: list[dict[str, Any]],
    conversation_history: Any,
    api_call_count: int,
    *,
    abort_message: str,
    interrupt_text: str,
) -> dict[str, Any]:
    """Announce ``abort_message``, close any open tool sequence with ``interrupt_text``,
    persist, clear the interrupt and return the ``interrupted`` result dict."""
    _vlines(agent, f"⚡ {abort_message}")
    close_interrupted_tool_sequence(messages, interrupt_text)
    agent._persist_session(messages, conversation_history)
    # The turn was stopped, not rebuilt: a pending steer was aimed at this turn's next
    # tool iteration, which will no longer happen — drop it (hard-cancel semantics).
    agent.clear_interrupt(hard_cancel=True)
    return {
        "final_response": interrupt_text,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "interrupted": True,
    }


def interruptible_backoff_sleep(
    agent: Any,
    wait_time: float,
    _retry: TurnRetryState | None,
    *,
    messages: list[dict[str, Any]],
    conversation_history: Any,
    api_call_count: int,
    abort_message: str,
    interrupt_text: str,
    activity_label: str,
) -> dict[str, Any] | None:
    """Sleep ``wait_time`` in 200 ms slices so interrupts are honoured promptly, touching
    activity every ~30 s so the gateway's inactivity monitor knows we are alive.

    On interrupt with ``_retry`` given and a redirect pending: preserve the redirect, arm
    ``_retry.restart_with_redirected_messages`` and return ``None`` (caller rebuilds the
    turn). Otherwise return the ``interrupted`` result dict. ``None`` when the wait completed."""
    sleep_end = time.time() + wait_time
    _touch_counter = 0
    while time.time() < sleep_end:
        if agent._interrupt_requested:
            if _retry is not None and agent.clear_interrupt(preserve_redirect=True):
                _retry.restart_with_redirected_messages = True
                return None
            return abort_turn_on_interrupt(
                agent,
                messages,
                conversation_history,
                api_call_count,
                abort_message=abort_message,
                interrupt_text=interrupt_text,
            )
        time.sleep(0.2)
        _touch_counter += 1
        if _touch_counter % 150 == 0:  # 150 × 0.2s = 30s
            agent._touch_activity(f"{activity_label}, {int(sleep_end - time.time())}s remaining")
    return None


_ZAI_POLICY_NOTES = {
    "zai_coding_overload_long": " (Z.AI Coding overload adaptive long backoff)",
    "zai_coding_overload_short": " (Z.AI Coding overload short retry)",
}


def reset_hint(api_error: Exception) -> str:
    """``"~13m"`` until the ``reset_at`` parsed from *api_error* (epoch s/ms or ISO-8601), else ``""``.

    A bare "Rate limited. Waiting 60s" hides the one fact that decides whether to wait or switch
    models (#26889): a per-minute throttle and a 13-minute plan window look identical without it."""
    from mertina.agent.agent_runtime_helpers import extract_api_error_context
    from mertina.agent.credential_pool import _parse_absolute_timestamp
    from mertina.agent.usage_pricing import format_duration_compact

    reset_at = extract_api_error_context(api_error).get("reset_at")
    if reset_at is None:
        return ""
    remaining = (_parse_absolute_timestamp(reset_at) or 0.0) - time.time()
    return f"~{format_duration_compact(remaining)}" if remaining >= 1 else ""


def compute_error_backoff(
    agent: Any,
    api_error: Exception,
    *,
    retry_count: int,
    max_retries: int,
    is_rate_limited: bool,
    is_zai_coding_overload: bool,
    base_url: Any,
    model: Any,
) -> float:
    """Pick the wait before the next API retry and announce it. Retry-After wins for
    rate limits and any other retryable error (capped at 600s: Anthropic Tier 1 buckets
    reset in ~171s, so a 120s cap re-tripped the limit); otherwise jittered backoff,
    replaced by the adaptive policy for 429s / Z.AI overloads. Normal retries are
    buffered; long Z.AI Coding waits surface immediately."""
    # Imported lazily so tests that patch ``agent.retry_utils.jittered_backoff`` /
    # ``adaptive_rate_limit_backoff`` (incl. the run_agent conftest fast-backoff fixture) intercept.
    from mertina.agent.retry_utils import (
        adaptive_rate_limit_backoff,
        jittered_backoff,
        parse_retry_after_seconds,
    )

    # Respect Retry-After on every retryable provider error, not just 429s. Retryable
    # 5xx responses (e.g. Cloudflare 520/524) also carry the header or a structured
    # ``retry_after`` problem-detail body field; ignoring either turns an origin
    # outage into a retry storm.
    _retry_after = parse_retry_after_seconds(
        getattr(getattr(api_error, "response", None), "headers", None)
    )
    if _retry_after is None:
        _error_body = getattr(api_error, "body", None)
        if isinstance(_error_body, dict):
            # Some providers nest it as error.retry_after (the same unwrap
            # extract_api_error_context uses), others put it at the top level.
            _nested = _error_body.get("error")
            _payload = _nested if isinstance(_nested, dict) else _error_body
            _retry_after = parse_retry_after_seconds(_payload.get("retry_after"))
    if _retry_after is not None:
        # Cap at 10 minutes. Anthropic Tier 1 input-token buckets reset in ~171s, so a 120s cap
        # caused us to retry before the actual reset window and re-trip the limit. 600s covers all
        # realistic provider reset windows while still rejecting pathological values. (#26293)
        _retry_after = min(_retry_after, 600)
        if _retry_after <= 0:
            # A zero/expired cooldown (retry-after: 0, or an HTTP-date in the
            # past, which the parser clamps to 0.0) carries no usable wait —
            # treat it as absent so we never hot-loop the provider.
            _retry_after = None
    wait_time = (
        _retry_after
        if _retry_after is not None
        else jittered_backoff(retry_count, base_delay=2.0, max_delay=60.0)
    )
    _backoff_policy = None
    _adaptive = is_rate_limited or is_zai_coding_overload
    if _adaptive and _retry_after is None:
        wait_time, _backoff_policy = adaptive_rate_limit_backoff(
            retry_count,
            base_url=str(base_url),
            model=model,
            error=api_error,
            default_wait=wait_time,
        )
    _reset = reset_hint(api_error) if _adaptive else ""
    _wait_reason = (
        "Provider overloaded" if is_zai_coding_overload and not is_rate_limited else "Rate limited"
    )
    if _adaptive:
        _policy_note = _ZAI_POLICY_NOTES.get(_backoff_policy or "", "")
        _rate_limit_status = (
            f"⏱️ {_wait_reason}.{f' Resets in {_reset}.' if _reset else ''} Waiting {wait_time:.1f}s "
            f"(attempt {retry_count + 1}/{max_retries}){_policy_note}..."
        )
        if _backoff_policy == "zai_coding_overload_long":
            agent._emit_diagnostic_status(_rate_limit_status)
        else:
            agent._buffer_diagnostic_status(_rate_limit_status)
    else:
        _retry_status = f"⏳ Retrying in {wait_time:.1f}s (attempt {retry_count}/{max_retries})..."
        if _retry_after is not None and _retry_after > 60:
            # A 5xx Retry-After can now reach the 600s cap; buffering that wait
            # would leave the user silent for minutes, so surface long provider
            # cooldowns immediately (mirrors the zai_coding_overload_long path).
            agent._emit_diagnostic_status(_retry_status)
        else:
            agent._buffer_diagnostic_status(_retry_status)
    # The buffered line only replays if every retry fails; the live status
    # line is the one thing the user sees meanwhile. Name the wait there so a
    # 60s backoff after a 5xx is not an anonymous spinner — this is transient
    # (rewritten by the next frame, cleared on recovery), so it does not add
    # the transcript chatter the buffer exists to avoid. The reset window
    # belongs here too: during the wait this line is the only place the user
    # can learn whether to sit it out or switch models.
    _live_reason = (
        f"{_wait_reason.lower()} — resets in {_reset}," if _reset else "waiting on provider —"
    )
    agent._emit_diagnostic_wait(
        f"⏳ {_live_reason} retrying in {wait_time:.0f}s (attempt {retry_count}/{max_retries})"
    )
    logger.warning(
        "Retrying API call in %ss (attempt %s/%s) %s policy=%s error=%s",
        wait_time,
        retry_count,
        max_retries,
        agent._client_log_context(),
        _backoff_policy or "default",
        api_error,
    )
    return wait_time


@dataclass
class ClassifiedErrorVerdict:
    """Outcome of ``route_classified_error``. ``action``: ``"return"`` (terminal result),
    ``"break"`` (restart armed on ``_retry``), ``"continue"`` (re-enter the retry loop; Nous
    guard re-check) or ``"fallthrough"`` (proceed to overflow / client-error / backoff
    handling). The remaining fields are loop locals the router rebound or computed."""

    action: str
    result: dict[str, Any] | None
    status_code: int | None
    messages: list[dict[str, Any]]
    active_system_prompt: Any
    conversation_history: Any
    retry_count: int
    max_retries: int
    compression_attempts: int
    provider_overflow_recovery_pending: bool
    is_rate_limited: bool
    wrapped_output_cap_budget: int | None
    is_zai_coding_overload: bool


_OVERFLOW_REASONS = frozenset(
    {
        FailoverReason.long_context_tier,
        FailoverReason.payload_too_large,
        FailoverReason.context_overflow,
    }
)


_RATE_LIMIT_REASONS = frozenset(
    {
        FailoverReason.rate_limit,
        FailoverReason.billing,
        FailoverReason.upstream_rate_limit,
    }
)


_TRANSPORT_FAILURE_REASONS = frozenset({FailoverReason.timeout, FailoverReason.overloaded})


_LONG_CONTEXT_TIER_CAP = 200000


def _cap_long_context_tier(agent: Any) -> int:
    """Cap the compressor's context window at the long-context tier limit; returns the
    previous ``context_length``."""
    compressor = agent.context_compressor
    old_ctx = compressor.context_length
    if old_ctx > _LONG_CONTEXT_TIER_CAP:
        compressor.update_model(
            model=agent.model,
            context_length=_LONG_CONTEXT_TIER_CAP,
            base_url=agent.base_url,
            api_key=getattr(agent, "api_key", ""),
            provider=agent.provider,
            api_mode=agent.api_mode,
        )
        # Context probing flags exist only on the built-in compressor (plugin engines
        # manage their own). Don't persist — a tier limit, not a model capability;
        # 1M should return if extra usage is enabled.
        if hasattr(compressor, "_context_probed"):
            compressor._context_probed = True
            compressor._context_probe_persistable = False
        agent._buffer_vprint(
            f"⚠️  Anthropic long-context tier "
            f"requires extra usage — reducing context: "
            f"{old_ctx:,} → {_LONG_CONTEXT_TIER_CAP:,} tokens"
        )
    return old_ctx


def _eager_fallback_status(classified: Any, is_upstream: bool, is_transport_failure: bool) -> str:
    """Status line announcing an eager fallback switch."""
    if is_upstream:
        _upstream_name = (classified.error_context or {}).get("upstream_provider", "aggregator")
        return f"⚠️ Upstream {_upstream_name} rate-limited — switching to fallback model..."
    if classified.reason == FailoverReason.billing:
        if classified.billing_unverified:
            # Ambiguous body — don't assert billing.
            return (
                "⚠️ Provider reported usage/credit exhaustion "
                "(unverified — may be a content-filter rejection) "
                "— switching to fallback provider..."
            )
        return "⚠️ Billing or credits exhausted — switching to fallback provider..."
    if is_transport_failure:
        return "⚠️ Provider unreachable — switching to fallback provider..."
    return "⚠️ Rate limited — switching to fallback provider..."


def _is_genuine_nous_rate_limit(
    agent: Any, api_error: Exception, error_context: Any, classified: Any = None
) -> bool:
    """Record a genuine account-level Nous 429 to the cross-session breaker; upstream
    capacity 429s (no exhausted bucket in headers or last-known state) are left alone.

    *error_context* is the turn's (``extract_api_error_context``); *classified* brings the
    classifier's own context, where a welcome-tier ``rate_limited`` refusal and its ``reset_at``
    live. A long welcome reset is an exhausted allowance whatever the headers say, and the one
    place the user is told that signing in lifts it."""
    _genuine = False
    try:
        from mertina.agent.nous_rate_guard import (
            is_genuine_nous_rate_limit,
            is_long_welcome_rate_limit,
            record_nous_rate_limit,
        )

        _err_resp = getattr(api_error, "response", None)
        _err_hdrs = getattr(_err_resp, "headers", None) if _err_resp else None
        from mertina.cli.anon_auth import is_anonymous_agent

        anonymous = is_anonymous_agent(agent)
        _classified_ctx = getattr(classified, "error_context", None) or {}
        # Only an anonymous request's fairshare body is an allowance verdict; named
        # requests keep the exhausted-bucket rule, whatever their host or body says.
        _genuine = (
            anonymous and is_long_welcome_rate_limit(_classified_ctx)
        ) or is_genuine_nous_rate_limit(headers=_err_hdrs, last_known_state=agent._rate_limit_state)
        if _genuine:
            _merged = {
                **(error_context if isinstance(error_context, dict) else {}),
                **_classified_ctx,
            }
            record_nous_rate_limit(headers=_err_hdrs, error_context=_merged, anonymous=anonymous)
        else:
            logger.info(
                "Nous 429 looks like upstream capacity "
                "(no exhausted bucket in headers or "
                "last-known state) -- not tripping "
                "cross-session breaker."
            )
    except Exception:
        pass
    return _genuine


def route_classified_error(
    agent: Any,
    api_error: Exception,
    classified: Any,
    _retry: TurnRetryState,
    *,
    error_msg: str,
    error_context: Any,
    recovered_with_pool: bool,
    base_url: Any,
    model: Any,
    messages: list[dict[str, Any]],
    api_messages: Any,
    system_message: Any,
    active_system_prompt: Any,
    conversation_history: Any,
    retry_count: int,
    max_retries: int,
    compression_attempts: int,
    max_compression_attempts: int,
    api_call_count: int,
    effective_task_id: Any,
) -> ClassifiedErrorVerdict:
    """Ordered (load-bearing) recovery steps between classification and overflow handling:
    compaction-disabled overflow → terminal error (output-cap errors exempt); Anthropic
    long-context tier 429 → cap at 200k and compress; eager fallback for rate-limit/billing
    (immediately) and transport failures (after 1 retry) unless credential-pool rotation may
    still recover (upstream-aggregator 429s always fall back); persistent 401/403 → fallback
    chain once; genuine Nous 429 → cross-session breaker + re-enter the loop exactly once."""
    from mertina.agent.conversation_compression import conversation_history_after_compression
    from mertina.agent.conversation_loop import _arm_fallback_restart, _ra
    from mertina.agent.model_metadata import estimate_request_tokens_rough

    _provider_overflow_recovery_pending = False
    is_rate_limited = False
    _wrapped_output_cap_budget = None
    _is_zai_coding_overload = False
    status_code = getattr(api_error, "status_code", None)

    def _verdict(action: str, result: dict[str, Any] | None = None) -> ClassifiedErrorVerdict:
        return ClassifiedErrorVerdict(
            action=action,
            result=result,
            status_code=status_code,
            messages=messages,
            active_system_prompt=active_system_prompt,
            conversation_history=conversation_history,
            retry_count=retry_count,
            max_retries=max_retries,
            compression_attempts=compression_attempts,
            provider_overflow_recovery_pending=_provider_overflow_recovery_pending,
            is_rate_limited=is_rate_limited,
            wrapped_output_cap_budget=_wrapped_output_cap_budget,
            is_zai_coding_overload=_is_zai_coding_overload,
        )

    def _fallback_break() -> ClassifiedErrorVerdict:
        nonlocal active_system_prompt, retry_count, compression_attempts
        active_system_prompt = _arm_fallback_restart(
            agent, api_messages, active_system_prompt, _retry
        )
        retry_count = 0
        compression_attempts = 0
        return _verdict("break")

    # ``compression.enabled: false`` forbids every automatic trigger, incl. these
    # overflow recovery paths; error out. Output-cap errors exempt.
    _is_output_cap_error = (
        is_output_cap_error(error_msg)
        or parse_available_output_tokens_from_error(error_msg) is not None
    )
    if (
        classified.reason in _OVERFLOW_REASONS
        and not getattr(agent, "compression_enabled", True)
        and not _is_output_cap_error
    ):
        agent._flush_status_buffer()
        _vlines(
            agent,
            "❌ The conversation is too long for the model and automatic shrinking is off (compression.enabled: false).",
            "   💡 Run /compress to shrink it now, /new to start fresh, "
            "pick a model with a bigger context window, or remove attachments.",
        )
        logger.error(
            f"{agent.log_prefix}Context overflow ({classified.reason.value}) with "
            f"auto-compaction disabled — not compressing."
        )
        agent._persist_session(messages, conversation_history)
        _final_response = site_copy("compression_disabled", model=agent.model)
        return _verdict(
            "return",
            stamp_failure(
                {
                    "final_response": _final_response,
                    "messages": messages,
                    "completed": False,
                    "api_calls": api_call_count,
                    "error": _final_response,
                    "partial": True,
                    "failed": True,
                    "compaction_disabled": True,
                },
                "context_overflow",
                False,
            ),
        )

    # Anthropic 429 "Extra usage is required for long context requests" is a
    # subscription-tier limit, not transient: cap at 200k and compress.
    if classified.reason == FailoverReason.long_context_tier:
        old_ctx = _cap_long_context_tier(agent)
        compression_attempts += 1
        if compression_attempts <= max_compression_attempts:
            original_len = len(messages)
            # Overhead-aware request size so recovery arms on the true request
            # (msgs + tools + system), not the tool-blind message count.
            messages, active_system_prompt = agent._compress_context(
                # Route the overhead-aware _real_tokens (computed above) into compression, not the bare
                # last_prompt_tokens — which is 0 in the no-usage fallback, hiding the true request size
                # from the engine's overflow guard (upstream PR #77169 review).
                messages,
                system_message,
                approx_tokens=estimate_request_tokens_rough(
                    api_messages, tools=agent.tools or None
                ),
                task_id=effective_task_id,
            )
            conversation_history = conversation_history_after_compression(
                agent, messages, conversation_history
            )
            if len(messages) < original_len or old_ctx > _LONG_CONTEXT_TIER_CAP:
                agent._buffer_diagnostic_status(
                    COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE.format(
                        new_ctx=_LONG_CONTEXT_TIER_CAP, old_ctx=old_ctx
                    )
                )
                time.sleep(2)
                # Provider proved the request doesn't fit the reduced window; row count
                # isn't proof the rebuilt one does. Recheck before the next call.
                _provider_overflow_recovery_pending = True
                _retry.restart_with_compressed_messages = True
                return _verdict("break")
        # Compression exhausted or didn't help: fall through to normal error handling.

    # Eager fallback: rate-limit/billing switch immediately (primary won't recover in
    # the retry window); transport errors get 1 retry first.
    is_rate_limited = classified.reason in _RATE_LIMIT_REASONS
    # Some relays wrap upstream output-cap 400s as 429 (rate_limit). Only the max_tokens
    # clamp fixes it. Parsed once; gates the eager-fallback exemption and overflow entry.
    # Relay-wrapped output-cap errors: some gateways wrap an upstream "[400]: max_tokens (...) exceeds
    # model's maximum output tokens (...)" as HTTP 429, which classifies as rate_limit. The failure is a
    # deterministic request-shape problem — falling back to another provider (or burning generic retries)
    # can't fix it, but the output-cap clamp below can, in one retry (#72281). Parse once here; the result
    # gates both the eager-fallback exemption and the widened is_context_length_error entry, and is reused
    # as available_out inside the handler.
    _wrapped_output_cap_budget = (
        parse_available_output_tokens_from_error(error_msg)
        if classified.reason == FailoverReason.rate_limit
        else None
    )
    _is_transport_failure = classified.reason in _TRANSPORT_FAILURE_REASONS
    # Z.AI overload 429s classify `overloaded`, which `is_rate_limited` excludes. Detect
    # directly so the long backoff runs, and raise the ceiling to reach it.
    _is_zai_coding_overload = is_zai_coding_overload_error(
        base_url=str(base_url), model=model, error=api_error
    )
    if _is_zai_coding_overload:
        max_retries = max(max_retries, zai_coding_overload_retry_ceiling())
    _should_fallback = (is_rate_limited and _wrapped_output_cap_budget is None) or (
        _is_transport_failure and retry_count >= 2
    )
    if _should_fallback and agent._fallback_index < len(agent._fallback_chain):
        # No eager fallback while credential pool rotation may recover. Exception: an
        # upstream-aggregator 429 — the pool can't help, always fall back.
        # Fixes #11314.
        _is_upstream = classified.reason == FailoverReason.upstream_rate_limit
        pool_may_recover = (
            False
            if _is_upstream
            else _ra()._pool_may_recover_from_rate_limit(agent._credential_pool)
        )
        if not pool_may_recover:
            agent._buffer_diagnostic_status(
                _eager_fallback_status(classified, _is_upstream, _is_transport_failure)
            )
            reset_at = error_context.get("reset_at") if isinstance(error_context, dict) else None
            if agent._try_activate_fallback(reason=classified.reason, reset_at=reset_at):
                return _fallback_break()

    # A 401/403 surviving credential refresh means a broken credential or endpoint:
    # escalate to the fallback chain once; False -> terminal handling.
    if (
        classified.is_auth
        and not _retry.auth_failover_attempted
        and agent._fallback_index < len(agent._fallback_chain)
    ):
        _retry.auth_failover_attempted = True
        agent._buffer_diagnostic_status(
            "🔐 Authentication failed and could not be refreshed — "
            "switching to fallback provider..."
        )
        if agent._try_activate_fallback(reason=classified.reason):
            return _fallback_break()

    # Nous Portal: a genuine account-level 429 is recorded to a shared file so ALL
    # sessions back off; is_genuine_nous_rate_limit excludes upstream 429s.
    if (
        is_rate_limited
        and agent.provider == "nous"
        and classified.reason == FailoverReason.rate_limit
        and not recovered_with_pool
        and _is_genuine_nous_rate_limit(agent, api_error, error_context, classified)
    ):
        # Re-enter the loop exactly once so the top-of-loop Nous guard runs
        # (retry_count = max_retries would skip it entirely).
        retry_count = max(0, max_retries - 1)
        return _verdict("continue")
    # Upstream capacity 429: normal retry logic will typically succeed.
    return _verdict("fallthrough")
