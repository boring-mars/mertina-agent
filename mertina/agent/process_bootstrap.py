# Ported from hermes-agent agent/process_bootstrap.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Process-level bootstrap helpers for ``run_agent``.

Lazy OpenAI SDK import (``_OpenAIProxy`` keeps ``isinstance`` and
``patch("agent.process_bootstrap.OpenAI")`` working), crash-resistant stdio
(``_SafeWriter``), env-only HTTP proxy resolution, and the httpcore backend that
runs sync httpx connects through the process-wide Happy Eyeballs racer
(``hermes_bootstrap``).
"""

from __future__ import annotations

_OPENAI_CLS_CACHE = None


def _load_openai_cls() -> type:
    """Import and cache ``openai.OpenAI``."""
    global _OPENAI_CLS_CACHE
    if _OPENAI_CLS_CACHE is None:
        from openai import OpenAI as _OPENAI_CLS_CACHE
    return _OPENAI_CLS_CACHE


class _OpenAIProxy:
    """Module-level proxy that looks like ``openai.OpenAI`` but imports lazily."""

    __slots__ = ()

    def __call__(self, *args, **kwargs):
        return _load_openai_cls()(*args, **kwargs)

    def __instancecheck__(self, obj):
        return isinstance(obj, _load_openai_cls())

    def __repr__(self):
        return "<lazy openai.OpenAI proxy>"


# Drop-in for ``openai.OpenAI``.
OpenAI = _OpenAIProxy()


__all__ = [
    "OpenAI",
    "_OpenAIProxy",
    "_SafeWriter",
    "_get_proxy_for_base_url",
    "_get_proxy_from_env",
    "_install_safe_stdio",
    "_load_openai_cls",
    "build_keepalive_http_client",
    "close_shared_transports",
    "enable_happy_eyeballs_on_client",
]
