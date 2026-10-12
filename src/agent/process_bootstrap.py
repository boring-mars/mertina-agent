"""Process-level bootstrap helpers for ``run_agent``.

Lazy OpenAI SDK import (``_OpenAIProxy`` keeps ``isinstance`` and
``patch("agent.process_bootstrap.OpenAI")`` working), crash-resistant stdio
(``_SafeWriter``), env-only HTTP proxy resolution, and the httpcore backend that
runs sync httpx connects through the process-wide Happy Eyeballs racer
(``hermes_bootstrap``).
"""


import sys

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


class _SafeWriter:
    """Transparent stdio wrapper swallowing OSError/ValueError from broken pipes.

    Headless runs (systemd, Docker) lose the stdout pipe → ``OSError: [Errno 5]``;
    subagent threads can see the shared handle close → ``ValueError``. Either
    would otherwise crash the agent (often via double-fault in an except handler).
    """

    __slots__ = ("_inner",)

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def write(self, data):
        try:
            return self._inner.write(data)
        except (OSError, ValueError):
            return len(data) if isinstance(data, str) else 0

    def flush(self):
        try:
            self._inner.flush()
        except (OSError, ValueError):
            pass

    def fileno(self):
        return self._inner.fileno()

    def isatty(self):
        try:
            return self._inner.isatty()
        except (OSError, ValueError):
            return False

    def __getattr__(self, name):
        return getattr(self._inner, name)
































def _install_safe_stdio() -> None:
    """Wrap stdout/stderr so best-effort console output cannot crash the agent."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and not isinstance(stream, _SafeWriter):
            setattr(sys, stream_name, _SafeWriter(stream))


# Drop-in for ``openai.OpenAI``.
OpenAI = _OpenAIProxy()



