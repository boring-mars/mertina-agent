# Ported from hermes-agent agent/agent_init.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Implementation of :meth:`AIAgent.__init__` as ``init_agent(agent, ...)``.

``init_agent`` is a thin, ordered orchestrator over ``_init_*`` / ``_build_*`` phase
helpers (routing → client → tools → session → config sections). Phase ORDER is
load-bearing: later phases read attributes earlier ones set.
"""

from __future__ import annotations

import sys
from contextlib import suppress
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

from mertina.agent.iteration_budget import IterationBudget


def _cfg_dict(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    """``cfg[key]`` if it is a mapping, else ``{}`` (malformed sections are ignored)."""
    section = cfg.get(key, {})
    return section if isinstance(section, dict) else {}


def _resolve_api_mode(agent):
    """Set ``agent.api_mode``."""
    agent.api_mode = "chat_completions"


def _finalize_routing(agent):
    # Warm the transport cache so import errors surface at init (non-fatal: some modes lack one).
    with suppress(Exception):
        agent._get_transport()


def _set_defaults(agent, table: dict[str, Any]) -> None:
    """Assign each ``name -> value`` on ``agent``; callables are factories (fresh per agent)."""
    for name, value in table.items():
        setattr(agent, name, value() if callable(value) else value)


# Control-flow state (interrupts).
_CONTROL_STATE: dict[str, Any] = {
    "_interrupt_requested": False,
    "_interrupt_message": None,  # optional message that triggered the interrupt
}

# Session state.
_SESSION_STATE: dict[str, Any] = {
    # Cached system prompt (built once).
    "_cached_system_prompt": None,
}


def _explicit_client_kwargs(agent, api_key, base_url) -> dict[str, Any]:
    """OpenAI-client kwargs from explicit credentials."""
    _parsed_url = urlparse(base_url)
    client_kwargs = {"api_key": api_key, "base_url": base_url}
    if _parsed_url.query:
        client_kwargs["base_url"] = urlunparse(_parsed_url._replace(query=""))
        client_kwargs["default_query"] = {k: v[0] for k, v in parse_qs(_parsed_url.query).items()}
    return client_kwargs


def _init_openai_client(agent, api_key, base_url):
    """OpenAI-wire client: resolve kwargs, construct."""
    client_kwargs = _explicit_client_kwargs(agent, api_key, base_url)

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
    # LLM client for the chat-completions wire.
    _init_openai_client(agent, api_key, base_url)


def _load_tools(agent):
    from mertina import model_tools

    agent.tools = model_tools.get_tool_definitions(
        quiet_mode=agent.quiet_mode,
    )

    agent.valid_tool_names = (
        {tool["function"]["name"] for tool in agent.tools} if agent.tools else set()
    )
    if agent.quiet_mode:
        return
    if agent.tools:
        print(f"🛠️  Loaded {len(agent.tools)} tools: {', '.join(sorted(agent.valid_tool_names))}")
    else:
        print("🛠️  No tools loaded (all tools filtered out or unavailable)")


def _init_session_state(
    agent,
    session_id,
):
    agent.session_id = session_id
    _set_defaults(agent, _SESSION_STATE)


def _apply_agent_section(agent, _agent_cfg):
    _agent_section = _cfg_dict(_agent_cfg, "agent")

    # App-level API retry count (wraps each model API call). Default 3; 1 = single attempt.
    try:
        _api_retries = max(int(_agent_section.get("api_max_retries", 3)), 1)
    except (TypeError, ValueError):
        _api_retries = 3
    agent._api_max_retries = _api_retries


def _init_usage_state(agent):
    _set_defaults(agent, _USAGE_STATE)


# Per-session usage accounting.
_USAGE_STATE: dict[str, Any] = {
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
}

# Constructor params stored verbatim under the same name.
_PASSTHROUGH_PARAMS = (
    "model",
    "max_iterations",
    "verbose_logging",
    "quiet_mode",
    # Model response configuration (None = provider/model default)
    "max_tokens",
)


def init_agent(
    agent,
    base_url: str = None,
    api_key: str = None,
    provider: str = None,
    model: str = "",
    max_iterations: int = sys.maxsize,
    verbose_logging: bool = False,
    quiet_mode: bool = False,
    log_prefix: str = "",
    session_id: str = None,
    max_tokens: int = None,
):
    """Initialize the AI Agent (body of :meth:`AIAgent.__init__`).

    Non-obvious parameters:
      max_iterations: default unlimited (sys.maxsize).
    """
    _params = locals()
    for _name in _PASSTHROUGH_PARAMS:
        setattr(agent, _name, _params[_name])
    agent.iteration_budget = IterationBudget(max_iterations)
    # CLI replaces this with _cprint so raw ANSI status lines go through prompt_toolkit's
    # renderer (StdoutProxy would mangle them). None = builtins.print.
    agent._print_fn = None
    agent.log_prefix = f"{log_prefix} " if log_prefix else ""
    # Effective base URL for feature detection (prompt caching, reasoning, etc.)
    agent.base_url = base_url or ""
    provider_name = (
        provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
    )
    agent.provider = provider_name or ""
    _resolve_api_mode(agent)
    _finalize_routing(agent)

    agent.suppress_status_output = False

    _set_defaults(agent, _CONTROL_STATE)

    _build_client(agent, api_key, base_url)
    _load_tools(agent)
    _init_session_state(
        agent,
        session_id,
    )

    # Config is not ported yet (v0.1.2): every section takes its default.
    _agent_cfg = {}

    _apply_agent_section(agent, _agent_cfg)
    _init_usage_state(agent)


__all__ = ["init_agent"]
