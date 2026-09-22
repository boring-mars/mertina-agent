# Ported from hermes-agent agent/reasoning_params.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Assistant-message building and the ``reasoning_content`` echo-back families for ``AIAgent``
(upstream's home for the provider reasoning-parameter policy, which v0.1 does not port).
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""

from mertina.agent.lazy_forward import forward as _forward
from mertina.agent.message_sanitization import matches_reasoning_echo_family


class ReasoningParamsMixin:
    """Assistant-message building and reasoning echo-back (see module docstring)."""

    _build_assistant_message = _forward(
        "mertina.agent.chat_completion_helpers", "build_assistant_message"
    )

    def _needs_thinking_reasoning_pad(self) -> bool:
        """True when the provider enforces ``reasoning_content`` echo-back on tool-call replays (DeepSeek, Kimi,
        MiMo thinking all 400 without it). Cached per (provider, model, base_url), invalidated by
        ``switch_model()`` / ``_try_activate_fallback()`` — called ~16× per turn.

        DeepSeek v4 thinking and Kimi / Moonshot thinking both reject replays of assistant tool-call
        messages that omit ``reasoning_content`` (refs 15250, #17400). Xiaomi MiMo thinking mode has the
        same requirement.
        """
        key = (self.provider, self.model, getattr(self, "_base_url_lower", self.base_url))
        cached = getattr(self, "_thinking_pad_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        result = (
            self._needs_deepseek_tool_reasoning()
            or self._needs_kimi_tool_reasoning()
            or self._needs_mimo_tool_reasoning()
            or self._reasoning_echo_opt_in()
        )
        self._thinking_pad_cache = (key, result)
        return result

    def _reasoning_echo_opt_in(self) -> bool:
        """``model.reasoning_echo`` opt-in for the *current* provider (covers gateways the host rules miss);
        fallback activation swaps the flag and ``restore_primary_runtime()`` restores it."""
        return bool(getattr(self, "_reasoning_echo_flag", False))

    # Echo families are host/provider-driven, not model-name-driven: aggregators re-exporting Kimi reject the
    # echo. Rule table: ``message_sanitization._REASONING_ECHO_RULES``. Kimi deliberately passes the raw
    # provider and no model (its rule matches exact provider ids + hosts only).
    def _needs_kimi_tool_reasoning(self) -> bool:
        """True when the current provider is Kimi / Moonshot thinking mode."""
        return matches_reasoning_echo_family("kimi", self.provider, None, self.base_url)

    def _needs_deepseek_tool_reasoning(self) -> bool:
        """True when the current provider is DeepSeek thinking mode (omitting the echo is an HTTP 400).

        DeepSeek V4 thinking mode requires ``reasoning_content`` on every assistant tool-call turn; omitting
        it causes HTTP 400 when the message is replayed in a subsequent API request (#15250).
        """
        return matches_reasoning_echo_family(
            "deepseek", (self.provider or "").lower(), self.model, self.base_url
        )

    def _needs_mimo_tool_reasoning(self) -> bool:
        """True when the current provider is Xiaomi MiMo thinking mode."""
        return matches_reasoning_echo_family(
            "mimo", (self.provider or "").lower(), self.model, self.base_url
        )

    _copy_reasoning_content_for_api = _forward(
        "mertina.agent.agent_runtime_helpers", "copy_reasoning_content_for_api"
    )
