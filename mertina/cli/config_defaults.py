# Ported from hermes-agent hermes_cli/config_defaults.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Default configuration data for Mertina Agent: DEFAULT_CONFIG.

Pure-data leaf module — must not import from mertina.cli.config. Comments are the user-facing
docs of config.yaml.
"""

DEFAULT_CONFIG = {
    "model": "",
    "agent": {
        # Mertina-level retry attempts for API errors (connection drops, timeouts, 5xx) wrapping the
        # whole call. Set 1 for a single attempt; raise to tolerate longer provider hiccups.
        "api_max_retries": 3,
        # System-prompt guidance telling the model to call tools instead of describing actions.
        # "auto" = gpt/codex models; true/false = force for all models; or a list of model-name
        # substrings (e.g. ["gpt", "codex", "gemini", "qwen"]).
        "tool_use_enforcement": "auto",
        # Execution-discipline prompt block (tool persistence, tools for arithmetic/system facts,
        # read-back after external writes, count reconciliation, literal identifiers,
        # verification-gated completion). Chosen once per session by model name (byte-stable).
        # "auto" = gpt/codex/grok/deepseek/kimi/qwen/glm/minimax/mimo/mistral; true/false = force;
        # or a list of model-name substrings.
        "execution_guidance": "auto",
    },
    "web": {
        "backend": "",  # shared fallback for every web capability
        "search_backend": "",  # per-capability override for web_search (e.g. "ddgs")
    },
    # IANA timezone (e.g. "Asia/Kolkata", "America/New_York"). Empty = server-local time.
    "timezone": "",
}
