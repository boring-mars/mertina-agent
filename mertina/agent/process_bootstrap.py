# Ported from hermes-agent agent/process_bootstrap.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Process-level bootstrap helpers for ``run_agent``.

Lazy OpenAI SDK import (``_OpenAIProxy`` keeps ``isinstance`` and
``patch("agent.process_bootstrap.OpenAI")`` working).
"""

from __future__ import annotations

from typing import Any

_OPENAI_CLS_CACHE: type | None = None


def _load_openai_cls() -> type:
    """Import and cache ``openai.OpenAI``."""
    global _OPENAI_CLS_CACHE
    if _OPENAI_CLS_CACHE is None:
        from openai import OpenAI as _OPENAI_CLS_CACHE  # noqa: N814  # upstream idiom
    return _OPENAI_CLS_CACHE  # type: ignore[return-value]  # the import above sets it


class _OpenAIProxy:
    """Module-level proxy that looks like ``openai.OpenAI`` but imports lazily."""

    __slots__ = ()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _load_openai_cls()(*args, **kwargs)

    def __instancecheck__(self, obj: object) -> bool:
        return isinstance(obj, _load_openai_cls())

    def __repr__(self) -> str:
        return "<lazy openai.OpenAI proxy>"


# Drop-in for ``openai.OpenAI``.
OpenAI = _OpenAIProxy()


__all__ = [
    "OpenAI",
    "_OpenAIProxy",
    "_load_openai_cls",
]
