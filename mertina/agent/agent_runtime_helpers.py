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
from typing import Any

from mertina.utils import (
    base_url_host_matches,
)

logger = logging.getLogger(__name__)


def _ra():
    """Lazy ``run_agent`` reference for test-patch routing."""
    from mertina import run_agent

    return run_agent


def _provider_supplied_client(agent, client_kwargs: dict) -> Any | None:
    """Ask the registered ProviderProfile for a custom client, if any. Resolves by provider name,
    then by ``base_url`` prefix so a URL-only runtime (``acp://…``) still reaches its profile.
    A profile that raises is logged and skipped: a third-party plugin must not be able to take
    the turn down, it can only fail to provide a client."""
    try:
        from mertina.providers import get_provider_profile
    except Exception:
        return None
    profile = None
    provider_name = (getattr(agent, "provider", "") or "").strip()
    if provider_name:
        try:
            profile = get_provider_profile(provider_name)
        except Exception:
            profile = None
    if profile is None:
        base_url = str(client_kwargs.get("base_url", "") or "").strip()
        if base_url:
            profile = _profile_for_base_url(base_url)
    if profile is None:
        return None
    try:
        return profile.create_client(**client_kwargs)
    except Exception:
        _ra().logger.warning(
            "Provider profile %r failed to create a client; falling back to the standard client path",
            getattr(profile, "name", provider_name) or "?",
            exc_info=True,
        )
        return None


def _profile_for_base_url(base_url: str) -> Any | None:
    """Registered profile whose own base_url is a prefix of ``base_url`` (provider name did not
    resolve). Prefix, not equality: the replaced copilot-acp branch keyed on
    ``startswith("acp://copilot")``, so a path or user override under the same root must resolve."""
    try:
        from mertina.providers import list_providers

        candidates = list_providers()
    except Exception:
        return None
    target = base_url.rstrip("/").lower()
    for candidate in candidates or []:
        own = str(getattr(candidate, "base_url", "") or "").rstrip("/").lower()
        if own and (target == own or target.startswith(own + "/")):
            return candidate
    return None


def _ensure_copilot_headers(client_kwargs: dict) -> None:
    """Defense-in-depth: recovery/restore rebuild from a snapshot without re-running header
    wiring; a missing Copilot-Integration-Id causes model_not_available_for_integrator 400s.
    Only ADD missing keys, never override."""
    try:
        if base_url_host_matches(str(client_kwargs.get("base_url", "")), "githubcopilot.com"):
            from mertina.cli.models import copilot_default_headers

            existing = dict(client_kwargs.get("default_headers") or {})
            existing_lower = {k.lower() for k in existing}
            for hk, hv in copilot_default_headers().items():
                if hk.lower() not in existing_lower:
                    existing[hk] = hv
            client_kwargs["default_headers"] = existing
    except Exception:
        _ra().logger.debug("Copilot default-header guard skipped", exc_info=True)


def _gemini_native_client(agent, client_kwargs: dict, httpx_verify, *, reason: str, shared: bool):
    """Native Gemini client when the base_url is the Gemini API, else None."""
    from mertina.agent.gemini_native_adapter import GeminiNativeClient, is_native_gemini_base_url

    base_url = str(client_kwargs.get("base_url", "") or "")
    if not is_native_gemini_base_url(base_url):
        return None
    safe_kwargs = {
        k: v
        for k, v in client_kwargs.items()
        if k in {"api_key", "base_url", "default_headers", "timeout", "http_client"}
    }
    if "http_client" not in safe_kwargs:
        keepalive_http = agent._build_keepalive_http_client(base_url, verify=httpx_verify)
        if keepalive_http is not None:
            safe_kwargs["http_client"] = keepalive_http
    client = GeminiNativeClient(**safe_kwargs)
    _ra().logger.info(
        "Gemini native client created (%s, shared=%s) %s",
        reason,
        shared,
        agent._client_log_context(),
    )
    return client


def create_openai_client(agent, client_kwargs: dict, *, reason: str, shared: bool) -> Any:
    from mertina.agent.auxiliary_client import _validate_base_url, _validate_proxy_env_urls
    from mertina.agent.ssl_verify import resolve_httpx_verify

    # Treat client_kwargs as read-only: callers pass agent._client_kwargs, and in-place mutation
    # leaks into later requests (a torn-down httpx transport got reused).
    # Callers pass agent._client_kwargs (or shallow copies of it) in; any in-place mutation leaks back into
    # the stored dict and is reused on subsequent requests. #10933 hit this by injecting an httpx.Client
    # transport that was torn down after the first request, so the next request wrapped a closed transport
    # and raised "Cannot send a request, as the client has been closed" on every retry. The revert resolved
    # that specific path; this copy locks the contract so future transport/keepalive work can't reintroduce
    # the same class of bug.
    client_kwargs = dict(client_kwargs)
    try:
        from mertina.providers import get_provider_profile

        profile = get_provider_profile(getattr(agent, "provider", ""))
        if profile is not None:
            for key, value in profile.build_client_kwargs_extras(
                base_url=client_kwargs.get("base_url", "")
            ).items():
                client_kwargs.setdefault(key, value)
    except Exception:
        _ra().logger.debug("Provider client-kwargs hook skipped", exc_info=True)
    # The MoA virtual provider has no OpenAI wire endpoint; the facade *is* the client. Rebuild the
    # facade, never a native client (TypeError; relay re-wire).
    # Rebuilding a native OpenAI client while agent.provider == "moa" (client replacement, stream-retry pool
    # cleanup, credential rotation, fallback+restore) drops the facade: the next primary call either raises
    # a `_moa_prepared_request` TypeError (#78382) or, when _client_kwargs carry an unrelated relay
    # base_url, leaks the request to a foreign gateway. Rebuild the facade instead (build_moa_facade also
    # re-wires the reference relay, see #53802).
    if (getattr(agent, "provider", "") or "").strip().lower() == "moa":
        from mertina.agent.moa_loop import build_moa_facade

        return build_moa_facade(agent, getattr(agent, "model", None) or "default")
    ssl_ca_cert = client_kwargs.pop("ssl_ca_cert", None)
    ssl_verify_cfg = client_kwargs.pop("ssl_verify", None)
    httpx_verify = resolve_httpx_verify(
        ca_bundle=ssl_ca_cert,
        ssl_verify=ssl_verify_cfg,
        base_url=str(client_kwargs.get("base_url", "")),
    )
    _validate_proxy_env_urls()
    _validate_base_url(client_kwargs.get("base_url"))
    # Provider-supplied client (registration seam): a provider whose wire protocol is not
    # OpenAI-over-HTTP supplies its own client from ProviderProfile.create_client(). Consulted
    # before the built-in ladder so a profile registered from ~/.hermes/plugins/ or a pip entry
    # point can ship a transport without editing this function (what makes an out-of-tree ACP
    # provider possible). None (the default) falls through, so existing providers are unaffected.
    provider_client = _provider_supplied_client(agent, client_kwargs)
    if provider_client is not None:
        _ra().logger.info(
            "%s client created from provider profile (%s, shared=%s) %s",
            agent.provider,
            reason,
            shared,
            agent._client_log_context(),
        )
        return provider_client
    from mertina.agent.auxiliary_client import _GEMINI_NATIVE_PROVIDER_NAMES

    if agent.provider in _GEMINI_NATIVE_PROVIDER_NAMES:
        client = _gemini_native_client(
            agent, client_kwargs, httpx_verify, reason=reason, shared=shared
        )
        if client is not None:
            return client
    # TCP keepalives so dead provider connections are detected (~60s) instead of hanging in
    # CLOSE-WAIT. Injected into the local copy only, so each client gets its own httpx.Client;
    # pinned by tests/agent/test_create_openai_client_reuse.py and
    # test_sequential_chats_live.py. What IS shared across those per-client wrappers is the
    # connection pool: ``build_keepalive_http_client`` mounts a process-shared ``HTTPTransport``
    # behind a per-client view whose ``close()`` is a no-op for the pool, so a closed wrapper
    # never takes a sibling's (or the successor's) connections with it
    # (tests/agent/test_shared_http_transport.py).
    # Without this, a peer that drops mid-stream leaves the socket in a state where epoll_wait never fires,
    # ``httpx`` read timeout may not trigger, and the agent hangs until manually killed. Probes after 30s
    # idle, retry every 10s, give up after 3 → dead peer detected within ~60s. Safety against #10933: the
    # ``client_kwargs = dict(client_kwargs)`` above means this injection only lands in the local per-call
    # copy, never back into ``agent._client_kwargs``. Each ``_create_openai_client`` invocation therefore
    # gets its OWN fresh ``httpx.Client`` whose lifetime is tied to the OpenAI client it is passed to. When
    # the OpenAI client is closed (rebuild, teardown, credential rotation), the paired ``httpx.Client``
    # closes with it, and the next call constructs a fresh one — no stale closed transport can be reused.
    # Bedrock Mantle: the ``aws-sdk`` placeholder is a sentinel for IAM-chain auth, not a bearer token.
    # Every rebuild from bare ``{api_key, base_url}`` kwargs (switch_model, fallback restore, credential
    # rotation, request-scoped clients) must reinstall the SigV4 http_client or Mantle answers 401.
    if "bedrock-mantle." in str(client_kwargs.get("base_url") or ""):
        from mertina.agent.bedrock_adapter import configure_bedrock_openai_client_kwargs

        timeout = client_kwargs.get("timeout")
        configure_bedrock_openai_client_kwargs(
            client_kwargs,
            timeout=timeout if isinstance(timeout, (int, float)) else None,
        )
    if "http_client" not in client_kwargs:
        keepalive_http = agent._build_keepalive_http_client(
            client_kwargs.get("base_url", ""), verify=httpx_verify
        )
        if keepalive_http is not None:
            client_kwargs["http_client"] = keepalive_http
    # Retries belong to the outer conversation loop (honors Retry-After); SDK retries would
    # double-retry inside it. auxiliary_client keeps SDK retries as it isn't wrapped.
    # Delegate all rate-limit / 5xx retry to hermes's outer conversation loop, which honors Retry-After and
    # applies adaptive/jittered backoff. The OpenAI SDK default (max_retries=2) uses its own 1-2s backoff
    # that ignores Retry-After and double-retries inside our loop — the same deadlock the Anthropic clients
    # hit (#26293). This is the single chokepoint every primary OpenAI/aggregator client passes through
    # (init, switch_model, recovery, restore, request-scoped); auxiliary_client builds its own clients and
    # keeps SDK retries because it is NOT wrapped by the conversation loop.
    client_kwargs.setdefault("max_retries", 0)
    _ensure_copilot_headers(client_kwargs)
    # All primary construction and recovery paths must identify Hermes to the official Codex
    # endpoint, including snapshots with custom header overrides.
    from mertina.agent.codex_headers import apply_required_codex_headers

    apply_required_codex_headers(
        client_kwargs,
        access_token=client_kwargs.get("api_key", ""),
        base_url=str(client_kwargs.get("base_url", "")),
    )
    # ``process_bootstrap.OpenAI`` is a lazy SDK proxy; resolved at call time so tests can patch it.
    from mertina.agent import process_bootstrap

    client = process_bootstrap.OpenAI(**client_kwargs)
    # Routing proxies name the deployment they served in a response header (#54864).
    from mertina.agent.served_model import install_served_model_capture

    install_served_model_capture(agent, client)
    _ra().logger.info(
        "OpenAI client created (%s, shared=%s) %s", reason, shared, agent._client_log_context()
    )
    return client
