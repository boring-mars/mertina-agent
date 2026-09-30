# Ported from hermes-agent agent/turn_failure_copy.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""User-facing copy for terminal turn outcomes: the iteration-summary failure text."""

from __future__ import annotations

from typing import Any

from mertina.agent.error_classifier import FailoverReason
from mertina.constants import display_mertina_home


def provider_label_for(provider: Any) -> str:
    """Human-friendly provider name for chat copy (``"OpenRouter"``, ``"Nous Portal"``…)."""
    from mertina.cli.models import provider_label

    return provider_label(str(provider or ""))


_NEXT_STEPS_RETRY = "Wait a minute and send /retry, or switch models with /model."


# Lead sentence per classifier reason once retries and fallback are exhausted.
_EXHAUSTED_LEADS: dict[str, str] = {
    FailoverReason.rate_limit.value: "{label} rate-limited every one of {attempts} attempts",
    FailoverReason.upstream_rate_limit.value: "{label} rate-limited every one of {attempts} attempts",
    FailoverReason.overloaded.value: "{label} reported it was overloaded on all {attempts} attempts",
    FailoverReason.server_error.value: "{label} returned a server error on all {attempts} attempts",
    FailoverReason.timeout.value: "{label} didn't respond in time on any of {attempts} attempts",
}


_EXHAUSTED_DEFAULT_LEAD = "{label} didn't answer after {attempts} attempts"


# Terminal copy for a non-retryable provider rejection, keyed by classifier reason.
_NONRETRYABLE_COPY: dict[str, str] = {
    FailoverReason.model_not_found.value: (
        "Model '{model}' isn't available on {label}. Pick a different model with /model "
        "(or `mertina model` in a terminal).{prefix_hint}"
    ),
    FailoverReason.format_error.value: (
        "{label} rejected this request as malformed, so the model didn't answer. Start a clean "
        "session with /new or switch models with /model; if it keeps happening, run `mertina doctor`."
    ),
    FailoverReason.role_alternation.value: (
        "{label} requires user and assistant turns to strictly alternate and rejected this "
        "conversation's shape. Start a clean session with /new or switch models with /model."
    ),
    FailoverReason.ssl_cert_verification.value: (
        "Mertina couldn't verify {label}'s security certificate, so the connection was refused. "
        "This is usually a corporate proxy or an outdated certificate store on this computer — "
        "see the terminal or `{home}/logs/agent.log` for the exact fix, or try another provider "
        "with /model."
    ),
    FailoverReason.provider_policy_blocked.value: (
        "{label}'s account settings don't allow this model for your request, so it didn't "
        "answer. Check the provider's data/privacy settings, or switch models with /model."
    ),
    FailoverReason.upstream_blocked.value: (
        "A firewall/CDN in front of {label} blocked the request before it reached the model, so "
        "your key is probably fine. Set a custom User-Agent via the provider's extra_headers, check "
        "the proxy/WAF rules, or switch providers with /model."
    ),
}


_NONRETRYABLE_DEFAULT_COPY = (
    "{label} rejected the request and retrying won't help. Pick another model with /model, "
    "or check the details in `{home}/logs/agent.log`."
)


_AUTH_COPY: dict[str, str] = {
    "oauth": "{label} rejected your sign-in, so the model can't be reached. Sign in again: `{relogin}`.",
    "api_key": (
        "{label} rejected your API key, so the model can't be reached. Update it in "
        "Settings → Providers, or run `mertina setup` in a terminal."
    ),
}


# One-off outcome strings: deterministic loop exits that are NOT failure codes.
_ONE_OFF_COPY: dict[str, str] = {
    "max_iterations_no_summary": (
        "I ran out of steps for this turn ({limit} tool calls) before finishing, and couldn't "
        "produce a summary. Send `continue` to keep going, or raise `max_iterations` in your config."  # noqa: E501  # upstream's message
    ),
}


_SITE_COPY: dict[str, str] = {**_ONE_OFF_COPY}


def site_copy(code: str, **fields: Any) -> str:
    """Chat copy for a failure code or one-off loop outcome; unknown fields default to empty
    strings."""
    return _SITE_COPY[code].format_map(_Defaults(fields))


def exhausted_copy(
    reason: str, *, label: str, attempts: int, summary: str, reset_seconds: float | None = None
) -> str:
    """Chat copy once retries + fallback are exhausted (``max_retries_exhausted_result``). A rate
    limit whose reset window is known names it: an 8.6h plan quota is not "wait a minute" (#89401)."""
    lead = _EXHAUSTED_LEADS.get(reason, _EXHAUSTED_DEFAULT_LEAD).format(
        label=label, attempts=attempts
    )
    if reset_seconds is not None and reset_seconds >= 120:
        from mertina.agent.retry_utils import format_reset_window

        situation = (
            f"its usage limit resets in {format_reset_window(reset_seconds)}. "
            "Send /retry after that, or switch models with /model."
        )
    else:
        situation = f"it looks temporarily unavailable. {_NEXT_STEPS_RETRY}"
    return (
        f"{lead} — {situation} To avoid this in future, "
        f"add a backup provider with `mertina fallback add`.\n\nProvider said: {summary}"
    )


def oauth_relogin_command(provider: Any) -> str:
    """The exact re-login command for a rejected OAuth grant, naming the provider slug and the active
    named profile: a profile's credentials are its own (93889b770da), so a bare ``mertina auth`` from
    the root profile re-signs the wrong store and the goal judge, reading a bare 401, guesses which
    service revoked the token (#114012)."""
    from mertina.constants import profile_cli_selector

    slug = str(provider or "").strip().lower()
    if slug == "nous":
        return f"mertina {profile_cli_selector()}portal"
    return f"mertina {profile_cli_selector()}auth add {slug} --type oauth"


def nonretryable_copy(
    classified: Any,
    *,
    provider: Any,
    model: Any,
    summary: str,
    prefix_suggestion: str | None = None,
) -> str:
    """Chat copy for a terminal non-retryable rejection (auth, model missing, TLS, generic 4xx)."""
    label = provider_label_for(provider)
    if getattr(classified, "is_auth", False):
        from mertina.agent.error_surface import auth_kind

        template = _AUTH_COPY[auth_kind(str(provider or ""))]
    else:
        template = _NONRETRYABLE_COPY.get(classified.reason.value, _NONRETRYABLE_DEFAULT_COPY)
    prefix_hint = (
        f" If you typed the name yourself it may be missing its vendor prefix — did you mean "
        f"'{prefix_suggestion}'?"
        if prefix_suggestion
        else ""
    )
    body = template.format(
        label=label,
        model=model,
        home=display_mertina_home(),
        prefix_hint=prefix_hint,
        relogin=oauth_relogin_command(provider),
    )
    return f"{body}\n\nProvider said: {summary}"


class _Defaults(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""
