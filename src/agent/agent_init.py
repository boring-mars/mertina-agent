"""Implementation of :meth:`AIAgent.__init__` as ``init_agent(agent, ...)``.

``init_agent`` is a thin, ordered orchestrator over ``_init_*`` / ``_build_*`` phase
helpers (routing → callbacks → client → tools → session → config sections → compression →
context engine). Phase ORDER is load-bearing: later phases read attributes earlier ones set.
Symbols that tests patch on ``run_agent.*`` (``OpenAI``, ``get_tool_definitions``,
``logger``, …) are resolved through :func:`_ra` so the patch contract is preserved.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse, urlunparse

from agent.iteration_budget import IterationBudget, normalize_budget_warning_ratio
from agent.memory_manager import StreamingContextScrubber
from agent.session_activity import ActivityProvenance
from agent.process_bootstrap import _install_safe_stdio
from agent.think_scrubber import StreamingThinkScrubber
from hermes_cli.timeouts import get_provider_request_timeout
from hermes_state_ids import new_session_id

# Same logger name as run_agent so caplog/patches on "run_agent" see our records.
logger = logging.getLogger("run_agent")


# Deduped: the gateway builds a fresh AIAgent per message, so it would warn every turn.
_warned_unavailable_providers: set[str] = set()
































def _normalize_run_budget_seconds(value) -> Optional[float]:
    """Positive float or None (feature off). ``bool`` rejected: YAML ``true`` → 1s budget."""
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None  # NaN compares False → None









def _cfg_dict(cfg: Dict[str, Any], key: str) -> Dict[str, Any]:
    """``cfg[key]`` if it is a mapping, else ``{}`` (malformed sections are ignored)."""
    section = cfg.get(key, {})
    return section if isinstance(section, dict) else {}






def _resolve_api_mode(agent, api_mode, provider_name, base_url):
    agent.api_mode = "chat_completions"


def _finalize_routing(agent, api_mode, credential_pool):
    with suppress(Exception):
        agent._get_transport()



def _set_defaults(agent, table: Dict[str, Any]) -> None:
    """Assign each ``name -> value`` on ``agent``; callables are factories (fresh per agent)."""
    for name, value in table.items():
        setattr(agent, name, value() if callable(value) else value)


# Control-flow state (interrupts / steer / redirect / delegation / background review).
_CONTROL_STATE: Dict[str, Any] = {
    "_executing_tools": False,  # lets _vprint print while tools run with stream consumers on
    "_trim_after_tool_batch": False,  # a >=1 MB tool result was committed; trim once the batch unwinds
    # Interrupts. Hard cancellation is separate from redirect/message state; the Event makes
    # the cause atomic for auxiliary stream pollers.
    "_interrupt_requested": False,
    "_interrupt_message": None,  # optional message that triggered the interrupt
    "_hard_interrupt_requested": threading.Event,
    "_execution_thread_id": None,  # set at run_conversation() start
    "_interrupt_thread_signal_pending": False,
    "_client_lock": threading.RLock,
    "_model_request_active": threading.Event,
    # /steer: the drain hook appends the note to the last tool result after the current
    # batch — no interrupt, no new user turn (role alternation preserved).
    "_pending_steer": None,
    "_pending_steer_lock": threading.Lock,
    # Active-turn redirect: keep the valid turn prefix, cancel only the in-flight request,
    # rebuild the tail with the correction. Drained at a role-safe boundary.
    "_pending_redirect": None,
    "_pending_redirect_lock": threading.Lock,
    # Concurrent-tool worker tids: `_set_interrupt` on `_execution_thread_id` alone doesn't
    # reach ThreadPoolExecutor workers, so interrupt()/clear_interrupt() fan out to these.
    "_tool_worker_threads": set,
    "_tool_worker_threads_lock": threading.Lock,
    # Subagent delegation: depth (0 = top-level) and running children (interrupt propagation).
    "_delegate_depth": 0,
    "_active_children": list,
    "_active_children_lock": threading.Lock,
}

# Per-turn bookkeeping: budgets, activity tracking, rate-limit/credits telemetry.
_TURN_STATE: Dict[str, Any] = {
    # Intermediate pressure warnings made models give up early; ordinary conversations
    # remain opt-in. Dispatcher workers receive a bounded completion checkpoint.
    "_iteration_budget_warning_injected": False,
    "_budget_grace_call": False,
    "_run_budget_started_at": None,  # set by turn_context.prepare_turn when a budget is active
    "_run_budget_wrapup_injected": False,  # one-shot latch for the 80% wrap-up notice
    # Activity tracking (API call / tool / stream chunk) for the gateway timeout handler and
    # "still working" notifications. Named provenances are stamped only by compression writers.
    "_last_activity_ts": lambda: time.time(),
    "_last_activity_desc": "initializing",
    "_last_activity_provenance": ActivityProvenance.UNKNOWN,
    "_session_activity_last_persist_mono": 0.0,  # rate-limits durable SessionDB stamps
    "_current_tool": None,
    "_api_call_count": 0,
    # Registry generation of the tool snapshot (set in _load_tools): a late refresh rejects
    # a stale rebuild instead of clobbering a newer one.
    "_tool_snapshot_generation": 0,
}

# Session persistence state.
_SESSION_STATE: Dict[str, Any] = {
    "_session_messages": list,
    "_memory_write_origin": "assistant_tool",
    # Cached system prompt (built once, rebuilt on compression) + its cross-session-stable
    # prefix, kept separately only to place an early cache marker.
    "_cached_system_prompt": None,
    "_cached_system_prompt_static": None,
    # Close flush and turn-start flush can overlap; the durable marker lives on each message
    # dict, so its test-and-append is serialized per agent.
    "_session_persist_lock": threading.RLock,
    # CLI's just-accepted user dict, reused by turn setup so its durable marker survives a
    # close-persistence race.
    "_pending_cli_user_message": None,
    "_session_db_created": False,  # DB row deferred to run_conversation()
    # True on the background review fork: never persist or publish session lifecycle hooks,
    # so its harness turn can't hijack or appear under the live session.
    "_persist_disabled": False,
}

# Streaming delivery state.
_STREAM_STATE: Dict[str, Any] = {
    "_stream_callback": None,  # streaming TTS; set early so _vprint can reference it
    "_stream_needs_break": False,  # one "\n\n" before the next real text delta after tools
    # Stateful scrubbers: <memory-context> / thinking spans split across deltas defeat
    # per-delta regexes (both tags must be in one string).
    "_stream_context_scrubber": StreamingContextScrubber,
    "_stream_think_scrubber": StreamingThinkScrubber,
    "_current_streamed_assistant_text": "",  # so a later completed interim isn't re-sent
    "_delivered_interim_texts": set,  # interims this user turn (spans Codex continuations)
    # Single-writer guard for the delta sink: each attempt claims a monotonic writer token and
    # the sink drops chunks from threads holding a stale one, so a superseded stream can't
    # interleave with the retry's. Threads that never claimed are never fenced.
    "_stream_writer_lock": threading.Lock,
    "_stream_writer_token": 0,
    "_stream_writer_tls": threading.local,
    "_stream_writer_dropped": 0,
    # Set once a strict endpoint 400/422s on ``stream_options``; later streams omit it (#9705).
    "_stream_options_unsupported": False,
    # API-facing user message override when it differs from the persisted transcript (voice).
    "_persist_user_message_idx": None,
    "_persist_user_message_override": None,
    "_persist_user_message_timestamp": None,
}




def _init_turn_state(agent, run_budget_seconds):
    _set_defaults(agent, _TURN_STATE)
    # Wall-clock run budget per turn: constructor arg wins, else agent.run_budget_seconds
    # (in _apply_agent_section). None = fully off (no clock reads, injection, or capping).
    agent.run_budget_seconds = _normalize_run_budget_seconds(run_budget_seconds)


    # Quiet mode must NOT raise per-logger levels: isEnabledFor() runs before propagation and
    # would starve the root file handlers. Noise reduction belongs in hermes_logging.










def _explicit_client_kwargs(agent, api_key, base_url, _provider_timeout) -> Dict[str, Any]:
    """OpenAI-client kwargs from explicit CLI/gateway credentials (auth already resolved)."""
    _parsed_url = urlparse(base_url)
    client_kwargs = {"api_key": api_key, "base_url": base_url}
    if _parsed_url.query:
        client_kwargs["base_url"] = urlunparse(_parsed_url._replace(query=""))
        client_kwargs["default_query"] = {k: v[0] for k, v in parse_qs(_parsed_url.query).items()}
    if _provider_timeout is not None:
        client_kwargs["timeout"] = _provider_timeout
    return client_kwargs






def _init_openai_client(agent, api_key, base_url, _provider_timeout):
    """OpenAI-wire client: resolve kwargs, apply header/TLS policy, construct."""
    if not api_key or not base_url:
        raise RuntimeError("model.base_url and model.api_key are required")
    client_kwargs = _explicit_client_kwargs(agent, api_key, base_url, _provider_timeout)

    agent._client_kwargs = client_kwargs  # stored for rebuilding after interrupt
    agent.api_key = client_kwargs.get("api_key", "")
    agent.base_url = client_kwargs.get("base_url", agent.base_url)
    try:
        agent.client = agent._create_openai_client(client_kwargs, reason="agent_init", shared=True)
        if not agent.quiet_mode:
            print(f"🤖 AI Agent initialized with model: {agent.model}")
            if base_url:
                print(f"🔗 Using custom base URL: {base_url}")
    except Exception as e:
        raise RuntimeError(f"Failed to initialize OpenAI client: {e}")


def _build_client(agent, api_key, base_url):
    _provider_timeout = get_provider_request_timeout(agent.provider, agent.model)
    _init_openai_client(agent, api_key, base_url, _provider_timeout)








def _load_tools(agent, enabled_toolsets, disabled_toolsets):
    from agent.web_search_registry import register_provider
    from plugins.web.gateway.provider import GatewayWebSearchProvider
    search_model = agent.model
    try:
        import tomllib
        with open("mertina.toml", "rb") as config_file:
            search_model = tomllib.load(config_file).get("search", {}).get("model", search_model)
    except (OSError, ValueError):
        pass
    register_provider(GatewayWebSearchProvider(agent.base_url, agent.api_key, search_model))
    # Capture the registry generation FIRST so a concurrent refresh can detect staleness.
    try:
        from tools.registry import registry as _snapshot_registry
        agent._tool_snapshot_generation = _snapshot_registry._generation
    except Exception:
        agent._tool_snapshot_generation = 0
    import model_tools
    agent.tools = model_tools.get_tool_definitions(
        enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled_toolsets,
        quiet_mode=agent.quiet_mode,
    )
    agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools} if agent.tools else set()
    if agent.quiet_mode:
        return
    if agent.tools:
        print(f"🛠️  Loaded {len(agent.tools)} tools: {', '.join(sorted(agent.valid_tool_names))}")
        if enabled_toolsets:
            print(f"   ✅ Enabled toolsets: {', '.join(enabled_toolsets)}")
        if disabled_toolsets:
            print(f"   ❌ Disabled toolsets: {', '.join(disabled_toolsets)}")
    else:
        print("🛠️  No tools loaded (all tools filtered out or unavailable)")
    if agent.save_trajectories:
        print("📝 Trajectory saving enabled")
    if agent.ephemeral_system_prompt:
        prompt_preview = agent.ephemeral_system_prompt[:60] + "..." if len(agent.ephemeral_system_prompt) > 60 else agent.ephemeral_system_prompt
        print(f"🔒 Ephemeral system prompt: '{prompt_preview}' (not saved to trajectories)")
def _init_session_state(agent):
    agent.session_start = datetime.now()
    agent.session_id = new_session_id(agent.session_start)
    _set_defaults(agent, _SESSION_STATE)
    agent._session_db = None


def _apply_display_config(agent, _agent_cfg, platform):
    # show_commentary: Codex phase=commentary → interim path (true) or reasoning channel.
    agent.show_commentary = bool(_cfg_dict(_agent_cfg, "display").get("show_commentary", True))

    # Window (seconds) for the bounded /fast auto|cold modes (agent.fast_mode).
    agent.fast_auto_seconds = (_agent_cfg.get("agent") or {}).get("fast_auto_seconds", 60)

    # lmstudio_load_mode: "explicit" (preload via management API) or "jit" (Auto-Evict path).
    _model_section = _cfg_dict(_agent_cfg, "model")
    _load_mode = str(_model_section.get("lmstudio_load_mode", "explicit") or "explicit").strip().lower()
    agent.lmstudio_load_mode = _load_mode if _load_mode in {"explicit", "jit"} else "explicit"
    if agent.lmstudio_load_mode != _load_mode:
        logger.warning(
            "Invalid model.lmstudio_load_mode=%r; expected 'explicit' or 'jit'. Using explicit.",
            _model_section.get("lmstudio_load_mode"),
        )

    # model.streaming=false seeds _disable_streaming (the loop's runtime fallback) for
    # backends with broken streaming tool calls. Session-scoped; orthogonal to display.streaming.
    _streaming = str(_model_section.get("streaming", "true")).strip().lower()
    agent._disable_streaming = _streaming in {"false", "0", "no", "off"}
    if not agent._disable_streaming and _streaming not in {"true", "1", "yes", "on"}:
        logger.warning(
            "Invalid model.streaming=%r; expected a boolean. Using streaming (default).",
            _model_section.get("streaming"),
        )



















# Provider ids whose runtime is resolved first-hand (never a named custom provider).






































def _apply_agent_section(agent, _agent_cfg):
    # Skills config: nudge interval for skill creation reminders
    agent._skill_nudge_interval = 10

    _agent_section = _cfg_dict(_agent_cfg, "agent")
    agent.budget_warning_ratio = normalize_budget_warning_ratio(
        _agent_section.get("budget_warning_ratio")
    )
    # Both: "auto" (model-list match), true, false, or list of model substrings; independent
    # of each other (gates in agent/system_prompt.py).
    agent._tool_use_enforcement = _agent_section.get("tool_use_enforcement", "auto")
    agent._execution_guidance = _agent_section.get("execution_guidance", "auto")

    # Wall-clock run budget from config — only when the constructor arg was not given.
    if agent.run_budget_seconds is None:
        agent.run_budget_seconds = _normalize_run_budget_seconds(
            _agent_section.get("run_budget_seconds")
        )

    # Default-on boolean gates: anti-stall guards (notice-only), universal guidance toggles
    # (ALL models, unlike enforcement), the local toolchain probe, Bot Mode protocol section.
    for _key in (
        "stall_guards", "task_completion_guidance", "parallel_tool_call_guidance",
        "environment_probe", "bot_mode_protocol",
    ):
        setattr(agent, f"_{_key}", bool(_agent_section.get(_key, True)))

    # "Bot Chat" gate hint for hosts that defer the DB title write past the first prompt build.
    agent._session_title_hint = None

    # platform_hints: <platform>: {append|replace}, stored verbatim (agent/system_prompt.py).
    agent._platform_hint_overrides = _cfg_dict(_agent_cfg, "platform_hints")

    # App-level API retry count (wraps each model API call). Default 3; 1 = single attempt.
    try:
        _api_retries = max(int(_agent_section.get("api_max_retries", 3)), 1)
    except (TypeError, ValueError):
        _api_retries = 3
    agent._api_max_retries = _api_retries


def _init_memory(agent, _agent_cfg, skip_memory, platform):
    agent._memory_store = None
    agent._memory_nudge_interval = 10
    agent._turns_since_memory = 0
    agent._memory_manager = None


def _init_usage_state(agent):
    _set_defaults(agent, _USAGE_STATE)


# Per-session usage accounting.
_USAGE_STATE: Dict[str, Any] = {
    "_user_turn_count": 0,
    "_is_user_initiated_turn": False,  # Copilot x-initiator: first call of a user turn = "user"
    # Usage anchors (agent/usage_anchor.py): last response's exact usage + transcript
    # snapshot; invalidated on compaction/session switch so stale anchors never suppress compression.
    "_usage_anchor": None,
    "_turn_base_usage_anchor": None,
    "_request_pressure_anchored": False,  # whether the last pressure figure came from the anchor
    # Cumulative token usage for the session
    "session_prompt_tokens": 0,
    "session_completion_tokens": 0,
    "session_total_tokens": 0,
    "session_api_calls": 0,
    "session_input_tokens": 0,
    "session_output_tokens": 0,
    "session_cache_read_tokens": 0,
    "session_cache_write_tokens": 0,
    "session_reasoning_tokens": 0,
    "session_estimated_cost_usd": 0.0,
    "session_cost_status": "unknown",
    "session_cost_source": "none",
    # Status-bar latency/velocity history (last 10 calls), shared by loop + codex_runtime.
    "_api_latency_history": lambda: deque(maxlen=10),
    "_api_output_history": lambda: deque(maxlen=10),
}

# Constructor params stored verbatim under the same name.
_PASSTHROUGH_PARAMS = (
    "model", "max_iterations", "save_trajectories", "verbose_logging", "quiet_mode",
    "tool_progress_mode", "ephemeral_system_prompt", "platform", "skip_context_files",
    "load_soul_identity", "pass_session_id", "log_prefix_chars",
    # OpenRouter provider preferences
    "providers_allowed", "providers_ignored", "providers_order", "provider_sort",
    "provider_require_parameters", "provider_data_collection", "openrouter_min_coding_score",
    # Toolset filtering
    "enabled_toolsets", "disabled_toolsets",
    # Model response configuration (None = provider/model default)
    "max_tokens", "reasoning_config", "service_tier",
)
# Gateway identity params stored as ``agent._<name>``. gateway_session_key is the stable
# per-chat key (e.g. agent:main:telegram:dm:123).
_GATEWAY_IDENTITY_PARAMS = (
    "user_id", "user_id_alt", "user_name", "chat_id", "chat_name", "chat_type", "thread_id",
    "gateway_session_key",
)
_CALLBACK_PARAMS = (
    "tool_progress_callback", "tool_start_callback", "tool_complete_callback",
    "thinking_callback", "reasoning_callback", "clarify_callback",
    "read_terminal_callback", "read_preview_callback", "drive_preview_callback",
    "read_window_below_callback", "connection_callback", "tour_callback",
    "step_callback", "stream_delta_callback", "interim_assistant_callback",
    "status_callback", "notice_callback", "notice_clear_callback",
    "event_callback", "reaction_callback", "tool_gen_callback",
)


def init_agent(
    agent, base_url: str = None, api_key: str = None, provider: str = None, api_mode: str = None,
    acp_command: str = None, acp_args: list[str] | None = None, command: str = None,
    args: list[str] | None = None, model: str = "", max_iterations: int = sys.maxsize,
    enabled_toolsets: List[str] = None, disabled_toolsets: List[str] = None,
    save_trajectories: bool = False, verbose_logging: bool = False, quiet_mode: bool = False,
    tool_progress_mode: str = "all", ephemeral_system_prompt: str = None,
    log_prefix_chars: int = 100, log_prefix: str = "", providers_allowed: List[str] = None,
    providers_ignored: List[str] = None, providers_order: List[str] = None,
    provider_sort: str = None, provider_require_parameters: bool = False,
    provider_data_collection: str = None, openrouter_min_coding_score: Optional[float] = None,
    session_id: str = None, tool_progress_callback: callable = None,
    tool_start_callback: callable = None, tool_complete_callback: callable = None,
    thinking_callback: callable = None, reasoning_callback: callable = None,
    clarify_callback: callable = None, read_terminal_callback: callable = None,
    read_preview_callback: callable = None, drive_preview_callback: callable = None,
    read_window_below_callback: callable = None, connection_callback: callable = None,
    tour_callback: callable = None, step_callback: callable = None,
    stream_delta_callback: callable = None, interim_assistant_callback: callable = None,
    tool_gen_callback: callable = None, status_callback: callable = None,
    notice_callback: callable = None, notice_clear_callback: callable = None,
    event_callback: Optional[Callable[[str, dict], None]] = None,
    reaction_callback: Optional[Callable[[str], None]] = None, max_tokens: int = None,
    reasoning_config: Dict[str, Any] = None, service_tier: str = None,
    request_overrides: Dict[str, Any] = None, prefill_messages: List[Dict[str, Any]] = None,
    platform: str = None, user_id: str = None, user_id_alt: str = None, user_name: str = None,
    chat_id: str = None, chat_name: str = None, chat_type: str = None, thread_id: str = None,
    gateway_session_key: str = None, skip_context_files: bool = False,
    load_soul_identity: bool = False, skip_memory: bool = False,
    skip_background_review: bool = False, session_db=None, parent_session_id: str = None,
    iteration_budget: "IterationBudget" = None, run_budget_seconds: Optional[float] = None,
    fallback_model: Dict[str, Any] = None, credential_pool=None, checkpoints_enabled: bool = False,
    checkpoint_max_snapshots: int = 20, checkpoint_max_total_size_mb: int = 500,
    checkpoint_max_file_size_mb: int = 10, pass_session_id: bool = False,
    requested_provider: str = None, capabilities: Optional[Dict[str, bool]] = None, cwd: Optional[str] = None,
):
    """Initialize the AI Agent (body of :meth:`AIAgent.__init__`).

    Non-obvious parameters:
      max_iterations: default unlimited (sys.maxsize); the budget is shared with subagents.
      requested_provider: provider identity before runtime canonicalization.
      cwd: logical session workspace, available to memory providers during construction;
        None or empty leaves the runtime cwd resolver unpinned.
      openrouter_min_coding_score: coding-score floor for ``openrouter/pareto-code`` only.
      clarify_callback: ``(question, choices) -> str``; None → the clarify tool errors.
      reasoning_config: None → ``{"enabled": True, "effort": "medium"}`` on OpenRouter.
      prefill_messages: priming history. Anthropic Sonnet/Opus 4.6+ 400 on a trailing
        assistant message — use structured outputs there instead.
      skip_context_files: skip SOUL.md/.hermes.md/AGENTS.md/CLAUDE.md/.cursorrules injection;
        load_soul_identity keeps ~/.hermes/SOUL.md as identity regardless.
    """
    _install_safe_stdio()

    _params = locals()
    for _name in _PASSTHROUGH_PARAMS:
        setattr(agent, _name, _params[_name])
    for _name in _GATEWAY_IDENTITY_PARAMS:
        setattr(agent, f"_{_name}", _params[_name])
    agent.session_cwd = cwd or None
    # Shared iteration budget: parent creates, children inherit.
    agent.iteration_budget = iteration_budget or IterationBudget(max_iterations)
    # CLI replaces this with _cprint so raw ANSI status lines go through prompt_toolkit's
    # renderer (StdoutProxy would mangle them). None = builtins.print.
    agent._print_fn = None
    agent.background_review_callback = None  # Optional sync callback for gateway delivery
    agent.memory_notifications = "on"  # Memory update notifications: "off", "on", "verbose"
    # Skips the end-of-turn review fork (~30K tokens/event); one switch for both review paths.
    agent.skip_background_review = bool(skip_background_review)
    agent.log_prefix = f"{log_prefix} " if log_prefix else ""
    # Effective base URL for feature detection (prompt caching, reasoning, etc.)
    agent.base_url = base_url or ""
    agent._ollama_num_ctx: int | None = None
    provider_name = provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
    agent.provider = provider_name or ""
    agent.requested_provider = (
        requested_provider.strip().lower()
        if isinstance(requested_provider, str) and requested_provider.strip()
        else agent.provider
    )
    agent.capabilities = {
        key: value for key, value in (capabilities or {}).items()
        if isinstance(key, str) and isinstance(value, bool)
    }
    agent._credential_pool = credential_pool
    agent.acp_command = acp_command or command
    agent.acp_args = list(acp_args or args or [])
    _resolve_api_mode(agent, api_mode, provider_name, base_url)
    _finalize_routing(agent, api_mode, credential_pool)

    # Platform callbacks are stored under their parameter names verbatim.
    for _cb in _CALLBACK_PARAMS:
        setattr(agent, _cb, _params[_cb])
    agent.suppress_status_output = False

    _set_defaults(agent, _CONTROL_STATE)

    # reasoning_content echo opt-in; switch_model / fallback / restore keep it in sync.
    agent._reasoning_echo_flag = agent._read_reasoning_echo_from_config()
    agent.request_overrides = dict(request_overrides or {})
    agent.prefill_messages = prefill_messages or []  # Prefilled conversation turns
    agent._force_ascii_payload = False

    _init_turn_state(agent, run_budget_seconds)
    _set_defaults(agent, _STREAM_STATE)
    _build_client(agent, api_key, base_url)
    _load_tools(agent, enabled_toolsets, disabled_toolsets)
    _init_session_state(agent)

    # Load config once for memory, skills, and compression sections
    try:
        from hermes_cli.config import load_config_readonly as _load_agent_config
        _agent_cfg = _load_agent_config()
    except Exception:
        _agent_cfg = {}

    _apply_display_config(agent, _agent_cfg, platform)
    _init_memory(agent, _agent_cfg, skip_memory, platform)
    _apply_agent_section(agent, _agent_cfg)
    _init_usage_state(agent)


__all__ = ["init_agent"]


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.




# ---- END PLUGIN-COMPAT ----
