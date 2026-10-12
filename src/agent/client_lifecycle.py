"""Tool-resource teardown, wire-client lifecycle and credential refresh for ``AIAgent``.

``ClientLifecycleMixin`` owns task cleanup, the shared primary client, per-request client caches
(owner-thread close vs stranger-thread abort), credential rotation and route-derived headers.
"""
import logging
import threading
import time
from contextlib import suppress
from typing import Any, Optional

from agent.lazy_forward import forward as _forward, forward_static as _forward_static, lazy_attr as _lazy_attr
from hermes_cli.timeouts import get_provider_request_timeout
from utils import base_url_host_matches, env_float

logger = logging.getLogger("run_agent")  # origin module's logger name: log records / caplog filters unchanged
_QWEN_CODE_VERSION = "0.14.1"  # Qwen Portal mimics the QwenCode CLI
# Per-request cache slot attribute names (OpenAI-style and Anthropic clients).
_OPENAI_SLOT = "_request_client_cache"
_ANTHROPIC_SLOT = "_request_anthropic_client_cache"
_NO_SOCKETS_SUFFIX = " — no sockets found; in-flight request may keep running until the provider finishes"


def _routermint_headers() -> dict:
    """User-Agent RouterMint needs to avoid Cloudflare 1010 blocks."""
    from hermes_cli import __version__ as _HERMES_VERSION
    return {"User-Agent": f"HermesAgent/{_HERMES_VERSION}"}


def _qwen_portal_headers() -> dict:
    import platform as _plat
    _ua = f"QwenCode/{_QWEN_CODE_VERSION} ({_plat.system().lower()}; {_plat.machine()})"
    return {
        "User-Agent": _ua, "X-DashScope-CacheControl": "enable", "X-DashScope-UserAgent": _ua,
        "X-DashScope-AuthType": "qwen-oauth",
    }


# Route-specific default headers; first host match wins (order preserved from the original chain).
# Builders resolve their module lazily so run_agent keeps its import-time cost and avoids cycles.
_ROUTE_DEFAULT_HEADERS = (
    ("openrouter.ai", lambda self, url: _lazy_attr("agent.auxiliary_client", "build_or_headers")()),
    ("ai-gateway.vercel.sh", lambda self, url: dict(_lazy_attr("agent.auxiliary_client", "_AI_GATEWAY_HEADERS"))),
    ("integrate.api.nvidia.com", lambda self, url: _lazy_attr("agent.auxiliary_client", "build_nvidia_nim_headers")(url)),
    ("api.routermint.com", lambda self, url: _routermint_headers()),
    ("githubcopilot.com", lambda self, url: _lazy_attr("hermes_cli.models", "copilot_default_headers")()),
    ("api.kimi.com", lambda self, url: dict(_lazy_attr("agent.auxiliary_client", "_AI_GATEWAY_HEADERS"))),
    ("portal.qwen.ai", lambda self, url: _qwen_portal_headers()),
    ("chatgpt.com", lambda self, url: _lazy_attr("agent.codex_headers", "codex_cloudflare_headers")(
        self._client_kwargs.get("api_key", ""), base_url=url)),
    # Covers provider=xai and provider=xai-oauth (api.x.ai).
    ("x.ai", lambda self, url: _lazy_attr("tools.xai_http", "hermes_xai_default_headers")()),
)


def _reset_slot(cache: dict, *, in_use: bool = False) -> None:
    cache["client"] = None
    cache["key"] = None
    cache["poisoned"] = False
    cache["in_use"] = in_use






class ClientLifecycleMixin:

    def _client_log_context(self) -> str:
        thread = threading.current_thread()
        return (
            f"thread={thread.name}:{thread.ident} provider={getattr(self, 'provider', 'unknown')} "
            f"base_url={getattr(self, 'base_url', 'unknown')} model={getattr(self, 'model', 'unknown')}"
        )

    def _anthropic_log_context(self) -> str:
        return f"provider={getattr(self, 'provider', None)} model={getattr(self, 'model', None)}"

    def _openai_client_lock(self) -> threading.RLock:
        if getattr(self, "_client_lock", None) is None:
            self._client_lock = threading.RLock()
        return self._client_lock

    @staticmethod
    def _is_openai_client_closed(client: Any) -> bool:
        """Check if an OpenAI client is closed.

        Handles both property and method forms of is_closed:
        - httpx.Client.is_closed is a bool property
        - openai.OpenAI.is_closed is a method returning bool

        Prior bug: getattr(client, "is_closed", False) returned the bound method,
        which is always truthy, causing unnecessary client recreation on every call.
        """
        from unittest.mock import Mock

        if isinstance(client, Mock):
            return False

        is_closed_attr = getattr(client, "is_closed", None)
        if is_closed_attr is not None:
            # Handle method (openai SDK) vs property (httpx)
            if callable(is_closed_attr):
                if is_closed_attr():
                    return True
            elif bool(is_closed_attr):
                return True

        http_client = getattr(client, "_client", None)
        if http_client is not None:
            return bool(getattr(http_client, "is_closed", False))
        return False

    @staticmethod
    def _build_keepalive_http_client(base_url: str = "", *, verify: Any = True) -> Any:
        """Build the shared OpenAI httpx client used by main and aux paths."""
        from agent.process_bootstrap import build_keepalive_http_client
        return build_keepalive_http_client(base_url, verify=verify)

    _create_openai_client = _forward("agent.agent_runtime_helpers", "create_openai_client")
    _force_close_tcp_sockets = _forward_static("agent.agent_runtime_helpers", "force_close_tcp_sockets")
    _cleanup_dead_connections = _forward("agent.agent_runtime_helpers", "cleanup_dead_connections")
    _run_codex_stream = _forward("agent.codex_runtime", "run_codex_stream")
    _recover_with_credential_pool = _forward("agent.agent_runtime_helpers", "recover_with_credential_pool")

    def _close_openai_client(self, client: Any, *, reason: str, shared: bool) -> None:
        if client is None:
            return
        ctx = self._client_log_context()
        # Force-close TCP sockets first (no CLOSE-WAIT accumulation), then the graceful SDK close.
        force_closed = self._force_close_tcp_sockets(client)
        try:
            client.close()
            logger.info("OpenAI client closed (%s, shared=%s, tcp_force_closed=%d) %s", reason, shared, force_closed, ctx)
        except Exception as exc:
            logger.debug("OpenAI client close failed (%s, shared=%s) %s error=%s", reason, shared, ctx, exc)

    def _retire_shared_openai_client(self, client: Any, *, reason: str) -> None:
        """Ownership-safe retirement of a replaced shared OpenAI client: ``shutdown()`` sockets only, FDs to GC.

        ``close()`` releases FDs from the calling thread while other threads may still hold the fd in an SSL BIO;
        a recycled fd then gets a TLS record written into an unrelated file (SQLite-header corruption).

        The shared primary client has no single owning thread — worker threads from stale-killed attempts
        may still be unwinding their SSL BIOs, and the codex-direct / MoA paths stream on the shared client
        itself. If we release an FD while another thread's SSL layer still caches the raw integer fd, the
        kernel can recycle it into an unrelated ``open()`` (e.g. ``kanban.db``) and the unwinding TLS flush
        then writes an application-data record into that file — the SQLite-header corruption documented in
        #29507/#70773.
        """
        if client is None:
            return
        try:
            shutdown_count = self._force_close_tcp_sockets(client)
            logger.info(
                "Shared OpenAI client retired (%s, tcp_shutdown=%d, fd_release=deferred_to_gc) %s",
                reason, shutdown_count, self._client_log_context(),
            )
        except Exception as exc:
            logger.debug("Shared OpenAI client retire failed (%s) %s error=%s", reason, self._client_log_context(), exc)



    def _ensure_primary_openai_client(self, *, reason: str) -> Any:
        with self._openai_client_lock():
            client = getattr(self, "client", None)
            if client is not None and not self._is_openai_client_closed(client):
                return client
            try:
                new_client = self._create_openai_client(self._client_kwargs, reason=reason, shared=True)
            except Exception as exc:
                logger.warning(
                    "Failed to recreate closed OpenAI client (%s) %s error=%s", reason, self._client_log_context(), exc,
                )
                raise RuntimeError("Failed to recreate closed OpenAI client") from exc
            self.client = new_client
        logger.warning(
            "Detected closed shared OpenAI client; recreated before use (%s) %s", reason, self._client_log_context(),
        )
        self._close_openai_client(client, reason=f"replace:{reason}", shared=True)
        return new_client

    @staticmethod
    def _api_kwargs_have_image_parts(api_kwargs: dict) -> bool:
        """True when the outbound request still has native image parts (Chat ``messages`` / Responses ``input``)."""
        if not isinstance(api_kwargs, dict):
            return False

        def _contains_image(value: Any) -> bool:
            if isinstance(value, dict):
                return value.get("type") in {"image_url", "input_image"} or any(
                    _contains_image(v) for v in value.values()
                )
            return isinstance(value, list) and any(_contains_image(v) for v in value)
        return any(
            _contains_image(item)
            for field in ("messages", "input")
            if isinstance(api_kwargs.get(field), list)
            for item in api_kwargs[field]
        )

    # ------------------------------------------------------------------ per-request client slots
    # One single-slot cache per client kind: {"client", "key", "poisoned", "in_use"}. Reuse keeps the warm httpx
    # pool between sequential calls; ``in_use`` keeps concurrent calls off one pool; ``poisoned`` marks a pool
    # whose sockets were shut from a stranger thread (never reuse it).
    # Reuse reasons: closes from the FD-owning worker's own finally after a response — the only closes that
    # attest a healthy pool. Poisoning still wins.
    _REQUEST_CLIENT_REUSE_REASONS = frozenset({"request_complete", "stream_request_complete"})

    def _request_slot(self, slot_attr: str) -> dict:
        cache = getattr(self, slot_attr, None)  # lazy: tests build agents via AIAgent.__new__ without __init__
        if cache is None:
            setattr(self, slot_attr, cache := {})
            _reset_slot(cache)
        return cache

    def _checkout_request_slot(self, slot_attr: str, key: Any) -> tuple:
        """Return ``(reusable_client, stale_client)``; at most one is non-None."""
        with self._openai_client_lock():
            cache = self._request_slot(slot_attr)
            cached = cache["client"]
            if cached is None or cache["in_use"]:
                return None, None
            if not cache["poisoned"] and cache["key"] == key and not self._is_openai_client_closed(cached):
                cache["in_use"] = True
                return cached, None
            # Key changed / poisoned / externally closed — rebuild. in_use was False, so closing the stale
            # client from this thread is FD-safe (no worker owns it).
            _reset_slot(cache)
            return None, cached

    def _store_request_slot(self, slot_attr: str, client: Any, key: Any) -> None:
        with self._openai_client_lock():
            cache = self._request_slot(slot_attr)
            if cache["client"] is None:
                cache.update(client=client, key=key, poisoned=False, in_use=True)
            # else: a concurrent call holds the slot — hand this client out untracked (fully closed later).

    def _release_request_slot(self, slot_attr: str, client: Any, reason: str) -> bool:
        """Owner-thread release; True when the client stays cached (clean finish, not poisoned)."""
        with self._openai_client_lock():
            cache = self._request_slot(slot_attr)
            if cache["client"] is client:
                if reason in self._REQUEST_CLIENT_REUSE_REASONS and not cache["poisoned"]:
                    cache["in_use"] = False
                    return True
                _reset_slot(cache)
        return False

    def _take_request_slot(self, slot_attr: str) -> tuple:
        """Teardown: empty the slot and return ``(client, was_in_use)``."""
        with self._openai_client_lock():
            cache = getattr(self, slot_attr, None)
            client, in_use = (cache["client"], bool(cache["in_use"])) if cache else (None, False)
            if cache is not None:
                _reset_slot(cache)
        return client, in_use

    def _abort_request_slot_client(self, slot_attr: str, client: Any, *, reason: str) -> None:
        """Cross-thread abort (interrupt loop, stale detector): ``shutdown(SHUT_RDWR)`` without releasing FDs.

        ``close()`` from a non-owning thread races the live SSL BIO and corrupts unrelated FDs; shutdown unblocks
        the owner's recv/send so it closes from its own context. The slot is poisoned so the pool is never reused.
        """
        if client is None:
            return
        anthropic = slot_attr == _ANTHROPIC_SLOT
        label = "Anthropic" if anthropic else "OpenAI"
        context = self._anthropic_log_context() if anthropic else self._client_log_context()
        with self._openai_client_lock():
            cache = self._request_slot(slot_attr)
            if cache["client"] is client:
                cache["poisoned"] = True
        try:
            # Non-HTTP providers cancel without closing owner-thread file descriptors.
            if callable(getattr(type(client), "cancel", None)):
                client.cancel()
                return
            shutdown_count = self._force_close_tcp_sockets(client)
            # Zero sockets shut down means the worker stays blocked — WARN, not success.
            # tcp_force_closed=0 means the stranger-thread abort found no sockets to shut down — the worker
            # stays blocked in recv and the provider keeps the slot (#72975). Surface that as WARNING so it
            # cannot be mistaken for a successful abort in the logs.
            # See #72975.
            _log = logger.warning if shutdown_count == 0 else logger.info
            _log(
                "%s client aborted (%s, shared=False, tcp_force_closed=%d, deferred_close=stranger_thread) %s%s",
                label, reason, shutdown_count, context, _NO_SOCKETS_SUFFIX if shutdown_count == 0 else "",
            )
        except Exception as exc:
            logger.debug("%s client abort failed (%s, shared=False) %s error=%s", label, reason, context, exc)

    def _create_request_openai_client(self, *, reason: str, api_kwargs: Optional[dict] = None) -> Any:
        from unittest.mock import Mock
        primary_client = self._ensure_primary_openai_client(reason=reason)
        if self.provider == "moa" or isinstance(primary_client, Mock):
            return primary_client
        with self._openai_client_lock():
            request_kwargs = dict(self._client_kwargs)
        # No SDK retry loop: the outer loop owns retries/rotation/fallback, and SDK retries stretch a hung
        # request ~3x past our stale detector.
        request_kwargs["max_retries"] = 0
        is_copilot = base_url_host_matches(str(request_kwargs.get("base_url", "")), "githubcopilot.com")
        if is_copilot and self._api_kwargs_have_image_parts(api_kwargs or {}):
            from hermes_cli.copilot_auth import copilot_request_headers
            request_kwargs["default_headers"] = copilot_request_headers(is_agent_turn=True, is_vision=True)
        cached, stale = self._checkout_request_slot(_OPENAI_SLOT, request_kwargs)
        if cached is not None:
            return cached
        if stale is not None:
            self._close_openai_client(stale, reason=f"reuse_evict:{reason}", shared=False)
        client = self._create_openai_client(request_kwargs, reason=reason, shared=False)
        # Snapshot nested dicts (default_headers) so an aliased inner object can't mutate the cache key.
        snapshot = {k: dict(v) if isinstance(v, dict) else v for k, v in request_kwargs.items()}
        self._store_request_slot(_OPENAI_SLOT, client, snapshot)
        return client

    def _close_request_openai_client(self, client: Any, *, reason: str) -> None:
        if not self._release_request_slot(_OPENAI_SLOT, client, reason):
            self._close_openai_client(client, reason=reason, shared=False)

    def _close_cached_request_openai_client(self, *, reason: str) -> None:
        """Teardown hook: really close the cached per-request wire client."""
        client, in_use = self._take_request_slot(_OPENAI_SLOT)
        if client is None:
            return
        if in_use:
            # Checked out by a worker: close() here would release FDs from a stranger thread. Abort the
            # sockets; the worker's own finally does the real close.
            self._abort_request_openai_client(client, reason=f"{reason}_in_flight")
        else:
            self._close_openai_client(client, reason=reason, shared=False)

    def _abort_request_openai_client(self, client: Any, *, reason: str) -> None:
        self._abort_request_slot_client(_OPENAI_SLOT, client, reason=reason)









    # ------------------------------------------------------------------ credential refresh




    # Adopt a fresh key this many seconds before the one in hand expires. Wider than the store's
    # own refresh skew (120 s) so the keepalive has normally already minted the replacement.
    _NOUS_KEY_ADOPT_SKEW_S = 180











    # ------------------------------------------------------------------ route-derived client config





