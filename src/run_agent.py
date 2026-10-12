#!/usr/bin/env python3
"""AIAgent: the tool-calling agent runner (conversation loop, tool execution, session lifecycle).

    from run_agent import AIAgent
    agent = AIAgent(base_url="http://localhost:30000/v1", model="claude-opus-4-20250514")
    response = agent.run_conversation("Tell me about the latest Python updates")
"""

# hermes_bootstrap must be the very first import (UTF-8 stdio on Windows; no-op on POSIX).
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    pass  # partial `hermes update` — only skips the Windows UTF-8 stdio setup

import json
import logging
logger = logging.getLogger(__name__)
import os
import re
import sys
import time
import threading
import uuid
import warnings
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime

from hermes_constants import get_hermes_home




# Sources that label the human conversation an interactive UI transport hosts. A finite ``hermes chat -q`` /
# one-shot child spawned from such a session inherits HERMES_SESSION_SOURCE (the terminal tool bridges the
# session env into child processes) but is NOT that conversation: labelling it ``tui``/``desktop`` lists it
# in the TUI/WebUI pickers as a resumable chat and lets ``hermes -c`` in the TUI continue it (#112550).
# Automation sources (kanban, tool, cron, a2a, ...) are inherited on purpose.
_UI_TRANSPORT_SOURCES = frozenset({"tui", "desktop"})

# Finite non-interactive CLI runs (``hermes chat -q``/``--oneshot``, ``hermes -z``) get their own source so human
# pickers hide them without title/cwd heuristics; ``hermes -c`` still treats them as CLI history.
ONESHOT_SOURCE = "oneshot"
CLI_FAMILY_SOURCES = frozenset({"cli", ONESHOT_SOURCE})






from agent.iteration_budget import IterationBudget
from hermes_cli.timeouts import get_provider_request_timeout, get_provider_stale_timeout

_hermes_home = get_hermes_home()  # read by agent_init via _ra()._hermes_home
from model_tools import get_toolset_for_tool
from tools.interrupt import set_interrupt as _set_interrupt

from agent.client_lifecycle import ClientLifecycleMixin
from agent.stream_delivery import StreamDeliveryMixin
from agent.status_output import StatusOutputMixin
from agent.api_request_hooks import ApiRequestHooksMixin
from agent.api_error_summary import PROVIDER_STREAM_PARSE_MARKERS, ApiErrorSummaryMixin
from agent.interrupt_control import InterruptControlMixin
from agent.turn_explainers import TurnExplainersMixin
from agent.activity_tracking import ActivityTrackingMixin
from agent.rate_limit_credits import RateLimitCreditsMixin
from agent.turn_facade import TurnFacadeMixin
from agent.vision_message_prep import VisionMessagePrepMixin
from agent.reasoning_params import ReasoningParamsMixin
from agent.lazy_forward import forward as _forward, forward_static as _forward_static
from agent.model_metadata import is_local_endpoint
from agent.message_sanitization import (
    coalesce_tool_call_id as _sanitize_coalesce_tool_call_id,
    deterministic_call_id as _codex_deterministic_call_id,
    uniquify_tool_call_ids as _sanitize_uniquify_tool_call_ids,
)
from agent.codex_responses_adapter import (
    _derive_responses_function_call_id as _codex_derive_responses_function_call_id,
    _split_responses_tool_id as _codex_split_responses_tool_id,
)
from utils import base_url_host_matches, base_url_hostname, env_float, model_forces_max_completion_tokens


_MAX_TOOL_WORKERS = 8


# Spawn the OpenRouter pre-warm thread once per process, not per AIAgent (gateway thread leak).
_openrouter_prewarm_done = threading.Event()


def _quietly(fn: Callable, *args, **kwargs) -> None:
    """Run one teardown step, swallowing any exception so sibling steps still run."""
    try:
        fn(*args, **kwargs)
    except Exception:
        pass
















class AIAgent(
    ClientLifecycleMixin, StreamDeliveryMixin, StatusOutputMixin, ApiRequestHooksMixin, ApiErrorSummaryMixin,
    InterruptControlMixin, TurnExplainersMixin, ActivityTrackingMixin, RateLimitCreditsMixin,
    TurnFacadeMixin, VisionMessagePrepMixin, ReasoningParamsMixin,
):
    """AI Agent with tool calling capabilities."""


    @property
    def base_url(self) -> str:
        return self._base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url = value
        self._base_url_lower = value.lower() if value else ""
        self._base_url_hostname = base_url_hostname(value)

    def __init__(
        self,
        base_url: str = None, api_key: str = None, provider: str = None, api_mode: str = None,
        acp_command: str = None, acp_args: list[str] | None = None, command: str = None, args: list[str] | None = None,
        model: str = "",
        max_iterations: int = sys.maxsize,  # unlimited tool-calling iterations by default (shared with subagents)
        tool_delay: float = None,  # deprecated: accepted for compatibility, ignored
        enabled_toolsets: List[str] = None, disabled_toolsets: List[str] = None,
        save_trajectories: bool = False, verbose_logging: bool = False, quiet_mode: bool = False,
        tool_progress_mode: str = "all", ephemeral_system_prompt: str = None,
        log_prefix_chars: int = 100, log_prefix: str = "",
        providers_allowed: List[str] = None, providers_ignored: List[str] = None, providers_order: List[str] = None,
        provider_sort: str = None, provider_require_parameters: bool = False, provider_data_collection: str = None,
        openrouter_min_coding_score: Optional[float] = None,
        session_id: str = None,
        tool_progress_callback: callable = None, tool_start_callback: callable = None,
        tool_complete_callback: callable = None, thinking_callback: callable = None,
        reasoning_callback: callable = None, clarify_callback: callable = None,
        read_terminal_callback: callable = None, read_preview_callback: callable = None,
        drive_preview_callback: callable = None, read_window_below_callback: callable = None,
        connection_callback: callable = None, tour_callback: callable = None, step_callback: callable = None,
        stream_delta_callback: callable = None, interim_assistant_callback: callable = None,
        tool_gen_callback: callable = None, status_callback: callable = None,
        notice_callback: callable = None, notice_clear_callback: callable = None,
        event_callback: Optional[Callable[[str, dict], None]] = None,
        reaction_callback: Optional[Callable[[str], None]] = None,
        max_tokens: int = None, reasoning_config: Dict[str, Any] = None, service_tier: str = None,
        request_overrides: Dict[str, Any] = None, prefill_messages: List[Dict[str, Any]] = None,
        platform: str = None, user_id: str = None, user_id_alt: str = None, user_name: str = None,
        chat_id: str = None, chat_name: str = None, chat_type: str = None, thread_id: str = None,
        gateway_session_key: str = None,
        skip_context_files: bool = False, load_soul_identity: bool = False,
        skip_memory: bool = False, skip_background_review: bool = False,
        session_db=None, parent_session_id: str = None,
        iteration_budget: "IterationBudget" = None, run_budget_seconds: Optional[float] = None,
        fallback_model: Dict[str, Any] = None, credential_pool=None,
        checkpoints_enabled: bool = False, checkpoint_max_snapshots: int = 20,
        checkpoint_max_total_size_mb: int = 500, checkpoint_max_file_size_mb: int = 10,
        pass_session_id: bool = False, requested_provider: str = None,
        capabilities: Dict[str, bool] | None = None, cwd: str | None = None,
    ):
        """Forwarder — see ``agent.agent_init.init_agent`` (same keyword parameters, minus ``tool_delay``)."""
        init_kwargs = {k: v for k, v in locals().items() if k not in ("self", "tool_delay")}
        if tool_delay is not None:
            warnings.warn("tool_delay is deprecated and ignored; sequential tool calls "
                          "no longer sleep between executions.", DeprecationWarning, stacklevel=2)
        from agent.agent_init import init_agent
        init_agent(self, **init_kwargs)









    switch_model = _forward("agent.agent_runtime_helpers", "switch_model")


    _stream_diag_init = _forward_static("agent.stream_diag", "stream_diag_init")
    _stream_diag_capture_response = _forward("agent.stream_diag", "stream_diag_capture_response")

    def _is_provider_stream_parse_error(self, error: BaseException) -> bool:
        """True for a malformed Anthropic event-stream frame (surfaced by the SDK as a plain ``ValueError``);
        that is wire trouble, not local validation, so it follows the truncated-JSON retry path."""
        return (getattr(self, "api_mode", None) == "anthropic_messages" and isinstance(error, ValueError)
                and not isinstance(error, (UnicodeEncodeError, json.JSONDecodeError))
                and any(marker in str(error).strip().lower() for marker in PROVIDER_STREAM_PARSE_MARKERS))





    def _hostname_for(self, base_url: Optional[str]) -> str:
        """Hostname of ``base_url``, or of the agent's own base URL when None."""
        if base_url is not None:
            return base_url_hostname(base_url)
        return getattr(self, "_base_url_hostname", "") or base_url_hostname(getattr(self, "_base_url_lower", ""))

    def _is_direct_openai_url(self, base_url: str = None) -> bool:
        """Return True when a base URL targets OpenAI's native API."""
        return self._hostname_for(base_url) == "api.openai.com"

    def _is_azure_openai_url(self, base_url: str = None) -> bool:
        """True when a base URL targets Azure OpenAI (standard client, but NO Responses API support)."""
        url = str(base_url).lower() if base_url is not None else (getattr(self, "_base_url_lower", "") or "")
        return base_url_host_matches(url, "openai.azure.com")

    def _is_github_copilot_url(self, base_url: str = None) -> bool:
        """Return True when a base URL targets GitHub Copilot's OpenAI-compatible API."""
        hostname = self._hostname_for(base_url)
        return bool(hostname) and (hostname == "api.githubcopilot.com" or hostname.endswith(".githubcopilot.com"))

    def _resolved_api_call_timeout(self) -> float:
        """Per-call request timeout: per-model ``timeout_seconds`` > provider ``request_timeout_seconds`` >
        ``HERMES_API_TIMEOUT`` > 1800s."""
        cfg = get_provider_request_timeout(self.provider, self.model)
        return cfg if cfg is not None else env_float("HERMES_API_TIMEOUT", 1800.0)

    def _resolved_api_call_stale_timeout_base(self) -> tuple[float, bool]:
        """Base non-stream stale timeout: per-model ``stale_timeout_seconds`` > provider-wide >
        ``HERMES_API_CALL_STALE_TIMEOUT`` > reasoning floor > 90s.

        Returns ``(seconds, uses_implicit_default)``; the implicit flag lets callers auto-disable the detector
        for local endpoints only when the user configured nothing.
        """
        cfg = get_provider_stale_timeout(self.provider, self.model)
        if cfg is not None:
            return cfg, False
        env_timeout = os.getenv("HERMES_API_CALL_STALE_TIMEOUT")
        if env_timeout is not None:
            return float(env_timeout), False
        # Reasoning-model floor (cloud gateways idle-kill mid-think); not "implicit" so the local-endpoint
        # short-circuit does not disable stale detection here.
        from agent.reasoning_timeouts import get_reasoning_stale_timeout_floor
        reasoning_floor = get_reasoning_stale_timeout_floor(self.model)
        if reasoning_floor is not None:
            return reasoning_floor, False
        return 90.0, True

    def _compute_non_stream_stale_timeout(self, api_payload: Any) -> float:
        """Effective non-stream stale timeout for ``api_payload`` (an ``api_kwargs`` dict or legacy ``messages``
        list), scaled by estimated context size and capped by the run budget."""
        stale_base, uses_implicit_default = self._resolved_api_call_stale_timeout_base()
        base_url = getattr(self, "_base_url", None) or self.base_url or ""
        if uses_implicit_default and base_url and is_local_endpoint(base_url):
            return float("inf")

        from agent.chat_completion_helpers import _high_effort_silence_floor, estimate_request_context_tokens
        est_tokens = estimate_request_context_tokens(api_payload)
        timeout = max(stale_base, 240.0) if est_tokens > 100_000 else max(stale_base, 150.0) if est_tokens > 50_000 else stale_base
        explicit = self._stale_timeout_is_explicit()
        # High-effort Codex reasoning (#112909) floors the IMPLICIT stale timeout before the run-budget
        # cap below, so the floor can never outlive the run budget.
        if self.api_mode == "codex_responses" and not explicit:
            timeout = max(timeout, _high_effort_silence_floor(self))
        # Run-budget cap: an implicit stale timeout is capped at half the remaining budget (>= 60s) so one
        # hung call cannot eat the run. Never raises the timeout; explicit user config still wins.
        run_budget = getattr(self, "run_budget_seconds", None)
        started = getattr(self, "_run_budget_started_at", None)
        if run_budget and started and not explicit:
            remaining = float(run_budget) - (time.time() - started)
            timeout = min(timeout, max(60.0, remaining * 0.5))
        return timeout

    def _stale_timeout_is_explicit(self) -> bool:
        """True when the user explicitly configured the stale timeout (config or env var); implicit values
        (reasoning floors, the 90s default) yield to the run-budget cap, explicit ones never do."""
        return (get_provider_stale_timeout(self.provider, self.model) is not None
                or os.getenv("HERMES_API_CALL_STALE_TIMEOUT") is not None)

    def _codex_silent_hang_hint(self, model: Optional[str] = None) -> Optional[str]:
        """Actionable hint when the request matches a known Codex silent-reject shape (currently the ``gpt-5.5``
        family: connection accepted, no events, no error), else None. Makes the stale timeout actionable."""
        if self.api_mode != "codex_responses":
            return None
        from agent.codex_responses_adapter import classify_responses_route

        if not classify_responses_route(self).is_codex_backend:
            return None
        eff_model = (model if model is not None else self.model) or ""
        # Match the gpt-5.5 family at word boundaries (bare, -codex, vendor-prefixed) but not gpt-5.50.
        if not re.search(r"(?:^|[/\-_])gpt-5\.5(?:$|[\-_])", eff_model.lower()):
            return None
        return (
            f"Codex backend appears to be silently rejecting {eff_model!r} "
            "on chatgpt.com/backend-api/codex (no stream events, no error). "
            "This is a known backend-side pattern that has affected ChatGPT "
            "Plus accounts intermittently. "
            "Workaround: try `gpt-5.4` on the same OAuth profile, "
            "or switch to a different model/provider in your fallback chain. "
            "Some ChatGPT Codex accounts do not support `gpt-5.4-codex`. "
            "See hermes-agent#21444 for symptom history."
        )

    def _is_openrouter_url(self) -> bool:
        """Return True when the base URL targets OpenRouter."""
        return base_url_host_matches(self._base_url_lower, "openrouter.ai")

    def _is_copilot_url(self) -> bool:
        """Return True when the base URL targets GitHub Copilot or GitHub Models."""
        return any(base_url_host_matches(self._base_url_lower, h) for h in ("api.githubcopilot.com", "models.github.ai"))

    def _is_copilot_provider(self) -> bool:
        """True when the active provider is GitHub Copilot under any alias (``copilot`` / ``github-copilot`` /
        ``github``) or by base URL; a bare equality check would silently skip credential recovery."""
        return (self.provider or "").strip().lower() in {"copilot", "github-copilot", "github"} or self._is_copilot_url()

    def _is_codex_backend(self) -> bool:
        """Return True for the ChatGPT OAuth Codex Responses backend."""
        return (getattr(self, "api_mode", None) == "codex_responses"
                and getattr(self, "_base_url_hostname", "") == "chatgpt.com"
                and "/backend-api/codex" in (getattr(self, "_base_url_lower", "") or ""))

    _direct_native_anthropic_tool_cache_capability = _forward("agent.agent_runtime_helpers", "_direct_native_anthropic_tool_cache_capability")

    @staticmethod
    def _model_requires_responses_api(model: str) -> bool:
        """True for GPT-5.x, which OpenAI and OpenRouter reject on /v1/chat/completions
        (``unsupported_api_for_model``)."""
        return model.lower().rsplit("/", 1)[-1].startswith("gpt-5")  # strip vendor prefix ("openai/gpt-5.4")


    def _max_tokens_param(self, value: int) -> dict:
        """``max_completion_tokens`` for newer OpenAI families (and Azure / Copilot serving them), else
        ``max_tokens``. URL-first, then model-name fallback for third-party endpoints fronting those models."""
        if (self._is_direct_openai_url() or self._is_azure_openai_url() or self._is_github_copilot_url()
                or model_forces_max_completion_tokens(self.model)):
            return {"max_completion_tokens": value}
        return {"max_tokens": value}

    @staticmethod
    def _requested_output_cap_from_api_kwargs(api_kwargs: Any) -> Optional[int]:
        """Extract the outgoing response token cap from a prepared request."""
        if not isinstance(api_kwargs, dict):
            return None
        for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
            try:
                value = int(api_kwargs.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def _has_content_after_think_block(self, content: str) -> bool:
        """True when text remains after stripping reasoning blocks (reasoning-only output is retried)."""
        return bool(content) and bool(self._strip_think_blocks(content).strip())

    _strip_think_blocks = _forward("agent.agent_runtime_helpers", "strip_think_blocks")

    @staticmethod
    def _has_natural_response_ending(content: str) -> bool:
        """Heuristic: does visible assistant text look intentionally finished?"""
        stripped = (content or "").rstrip()
        if not stripped:
            return False
        last = stripped[-1]
        # Closing punctuation/brackets, a fenced-code close, or an emoji (Misc Symbols, Dingbats, Emoticons, ...).
        return stripped.endswith("```") or last in '.!?:)"\']}。！？：）】」』》^' or ord(last) >= 0x1F300

    def _is_ollama_glm_backend(self) -> bool:
        """Ollama-hosted GLM models misreport finish_reason='stop'. Matches only explicit Ollama signatures
        (port 11434, "ollama" in URL, provider ollama), never arbitrary local proxies; excludes Ollama Cloud
        (``ollama.com`` / ``:cloud``), which reports faithfully — rewriting it would manufacture truncations.

        Crucially it does NOT match arbitrary local/private endpoints (LiteLLM/sglang/vLLM/LM Studio
        proxies, Tailscale boxes), which report finish_reason correctly and were the source of #13971's
        false-positive truncation continuations.
        Two signatures identify it: the ``ollama.com`` host (provider ``ollama-cloud``) and the ``:cloud``
        model suffix (cloud generation proxied through a local 11434 endpoint, #98406). Applying the
        stop→length rewrite to them manufactures false truncations and causes the continuation nudge to
        consume the model's output budget on the next retry, making further false-positives more likely.
        """
        model_lower = (self.model or "").lower()
        provider_lower = (self.provider or "").lower()
        if "glm" not in model_lower and provider_lower != "zai":
            return False
        base = self._base_url_lower
        # Ollama Cloud (hosted service or :cloud proxy) forwards finish_reason faithfully — do not rewrite.
        if "ollama.com" in base or ":cloud" in model_lower:
            return False
        if "ollama" in base or ":11434" in base:
            return True
        return provider_lower == "ollama"

    def _should_treat_stop_as_truncated(self, finish_reason: str, assistant_message, messages: Optional[list] = None) -> bool:
        """Detect conservative stop->length misreports for Ollama-hosted GLM models."""
        if finish_reason != "stop" or self.api_mode != "chat_completions" or not self._is_ollama_glm_backend():
            return False
        if not any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in (messages or [])):
            return False
        if assistant_message is None or getattr(assistant_message, "tool_calls", None):
            return False
        content = getattr(assistant_message, "content", None)
        if not isinstance(content, str):
            return False
        visible_text = self._strip_think_blocks(content).strip()
        if len(visible_text) < 20 or not re.search(r"\s", visible_text):
            return False
        return not self._has_natural_response_ending(visible_text)

    _extract_reasoning = _forward("agent.agent_runtime_helpers", "extract_reasoning")

    # Background memory/skill review — prompts live in agent.background_review.











    def close(self) -> None:
        """Release every resource this agent holds."""
        _quietly(self._drop_shared_client, lambda c: self._close_openai_client(c, reason="agent_close", shared=True))
        self._close_request_clients("agent_close")
        self._session_messages = []
        self._streamed_assistant_text_parts = []

    # -- close()/release_clients() phases -------------------------------------------------------------


    def _drop_shared_client(self, close_fn: Callable[[Any], None]) -> None:
        """Hand the shared OpenAI/httpx client to ``close_fn`` and clear the attribute."""
        # Retire the OpenAI/httpx client to release sockets immediately. #70773: eviction runs on the
        # gateway's memory-manager thread — a cross-thread hard close of the shared client can release TLS
        # FDs under a still-unwinding worker (FD-recycle → SQLite corruption). Retirement shuts the pooled
        # sockets down (the memory/socket win we want here) and lets GC release the FDs once no thread holds
        # them.
        client = getattr(self, "client", None)
        if client is not None:
            close_fn(client)
            self.client = None

    def _close_request_clients(self, reason: str) -> None:
        """Drop the cached per-request wire clients (reused across sequential LLM calls)."""
        _quietly(self._close_cached_request_openai_client, reason=reason)

    @property
    def is_interrupted(self) -> bool:
        """Check if an interrupt has been requested."""
        return self._interrupt_requested

    _build_system_prompt = _forward("agent.system_prompt", "build_system_prompt")

    # Call ID of a tool_call entry (dict or object); policy owner: ``message_sanitization.coalesce_tool_call_id``.
    _get_tool_call_id_static = staticmethod(_sanitize_coalesce_tool_call_id)

    @staticmethod
    def _get_tool_call_name_static(tc) -> str:
        """Function name of a tool_call entry (dict or object); Gemini requires it on every ``role: tool`` message."""
        if isinstance(tc, dict):
            fn = tc.get("function")
            return (fn.get("name", "") or "") if isinstance(fn, dict) else ""
        return getattr(getattr(tc, "function", None), "name", "") or ""

    _sanitize_api_messages = _forward_static("agent.agent_runtime_helpers", "sanitize_api_messages")

    @staticmethod
    def _is_thinking_only_assistant(msg: Dict[str, Any], *, drop_codex_reasoning_items: bool = True) -> bool:
        """True if ``msg`` is an assistant turn whose only payload is reasoning (no text, no tool_calls).

        Providers converting reasoning to thinking blocks reject it (400 "final block cannot be thinking"), so
        the turn is dropped from the API copy; the transcript keeps the reasoning block.
        """
        if not isinstance(msg, dict) or msg.get("role") != "assistant" or msg.get("tool_calls"):
            return False
        # Prefill stubs are thinking-only by construction; checked before content inspection since
        # repair_empty_non_final_messages may have healed content.
        if msg.get("_thinking_prefill"):
            return True
        if AIAgent._content_has_real_payload(msg.get("content")):
            return False
        # A native compaction checkpoint makes a carrier never thinking-only, regardless of api_mode or
        # reasoning field. Checked above every reasoning branch so no carrier shape is dropped.
        # The checkpoint is the server-side stand-in for already-pruned history and exists in exactly one
        # place; the codex_responses adapter also surfaces commentary text via msg["reasoning"], so the
        # string branch below would otherwise drop a carrier before the sidecar is ever inspected. See
        # #82108.
        from agent.native_compaction import has_compaction_checkpoint

        if has_compaction_checkpoint(msg.get("codex_reasoning_items")):
            return False
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        rd = msg.get("reasoning_details")
        if (isinstance(reasoning, str) and reasoning.strip()) or (isinstance(rd, list) and rd):
            return True
        # Codex Responses keeps encrypted reasoning under a separate key; only real items count as
        # thinking-only, empty/junk lists fall through to generic empty-turn handling.
        codex_items = msg.get("codex_reasoning_items")
        if drop_codex_reasoning_items and isinstance(codex_items, list):
            return any(isinstance(item, dict) and item.get("type") == "reasoning" for item in codex_items)
        return False

    @staticmethod
    def _content_has_real_payload(content: Any) -> bool:
        """True when assistant ``content`` carries anything beyond (redacted) thinking blocks / whitespace."""
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    if block:  # non-empty non-dict string etc.
                        return True
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if isinstance(text, str) and text.strip():
                        return True
                elif btype not in {"thinking", "redacted_thinking"}:
                    return True  # tool_use, image, document, etc. — real payload
            return False
        return content is not None and content != ""




    # Distinct ids per assistant turn, in place (policy owner: ``message_sanitization``). Collisions get a
    # deterministic ``<id>_d<n>`` suffix — never uuid4, for prompt-cache prefix stability.
    _uniquify_tool_call_ids = staticmethod(_sanitize_uniquify_tool_call_ids)

    _repair_tool_call = _forward("agent.agent_runtime_helpers", "repair_tool_call")

    # Codex Responses id policy (agent.codex_responses_adapter): deterministic call ids when the API omits one
    # (random UUIDs would break the provider prompt cache), split stored ids, derive valid ``fc_`` ids.
    _deterministic_call_id = staticmethod(_codex_deterministic_call_id)
    _split_responses_tool_id = staticmethod(_codex_split_responses_tool_id)
    _derive_responses_function_call_id = staticmethod(_codex_derive_responses_function_call_id)

    _interruptible_api_call = _forward("agent.chat_completion_helpers", "interruptible_api_call")
    _interruptible_streaming_api_call = _forward("agent.chat_completion_helpers", "interruptible_streaming_api_call")
    _try_activate_fallback = _forward("agent.chat_completion_helpers", "try_activate_fallback")


    _build_api_kwargs = _forward("agent.chat_completion_helpers", "build_api_kwargs")






    def _execute_tool_calls(self, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0) -> None:
        """Execute one tool sequentially or multiple independent tools concurrently."""
        tool_calls = assistant_message.tool_calls
        args = (assistant_message, messages, effective_task_id, api_call_count)
        self._executing_tools = True  # allow _vprint during tool execution even with stream consumers
        try:
            if len(tool_calls) <= 1:
                self._execute_tool_calls_sequential(*args)
            else:
                self._execute_tool_calls_concurrent(*args)
        finally:
            self._executing_tools = False
        # getattr: test stubs built without _set_defaults drive this method too
        if getattr(self, "_trim_after_tool_batch", False):
            # Only on normal completion: every executor frame that held a >=1 MB raw result has
            # unwound and just the spilled preview lives in ``messages``. An in-flight exception
            # would pin those frames via its traceback, so that path leaves the flag for the
            # next completed batch (agent/tool_executor.py, #70684).
            self._trim_after_tool_batch = False
            from hermes_cli.mem_trim import trim_memory
            trim_memory(reason="large tool result")


    _invoke_tool = _forward("agent.agent_runtime_helpers", "invoke_tool")

    @staticmethod
    def _wrap_verbose(label: str, text: str, indent: str = "     ") -> str:
        """Word-wrap verbose tool output to the terminal width (each existing line separately), continuation
        lines indented."""
        import shutil, textwrap
        wrap_width = max(40, shutil.get_terminal_size((120, 24)).columns - len(indent))
        out_lines: list[str] = []
        for raw_line in text.split("\n"):
            if len(raw_line) <= wrap_width:
                out_lines.append(raw_line)
            else:
                out_lines.extend(textwrap.wrap(raw_line, width=wrap_width, break_long_words=True, break_on_hyphens=False) or [raw_line])
        return f"{indent}{label}" + ("\n" + indent).join(out_lines)

    _execute_tool_calls_concurrent = _forward("agent.tool_executor", "execute_tool_calls_concurrent")
    _execute_tool_calls_sequential = _forward("agent.tool_executor", "execute_tool_calls_sequential")



def main(
    query: str = None, model: str = "", api_key: str = None,
    base_url: str = "", max_turns: int = 10,
):
    """Run one v0.1 conversation from the terminal."""
    from hermes_cli.config import load_config

    config = load_config()
    model_config = config.get("model", {})
    model = model or os.environ.get("MERTINA_MODEL") or model_config.get("name", "")
    api_key = (
        api_key or os.environ.get("MERTINA_API_KEY") or model_config.get("api_key")
    )
    base_url = (
        base_url or os.environ.get("MERTINA_BASE_URL")
        or model_config.get("base_url", "")
    )
    agent = AIAgent(
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_iterations=max_turns,
    )
    user_query = query if query is not None else input("You: ")
    result = agent.run_conversation(user_query)
    if result.get("final_response"):
        print(result["final_response"])
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Mertina v0.1 agent")
    parser.add_argument("query", nargs="?", help="message to send to the agent")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-key")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--max-turns", type=int, default=10)
    args = parser.parse_args()
    main(
        query=args.query,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        max_turns=args.max_turns,
    )
