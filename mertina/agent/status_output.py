# Ported from hermes-agent agent/status_output.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""User-facing status plumbing for ``AIAgent``: safe printing and quiet-mode gating.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""

from collections.abc import Callable
from typing import Any


class StatusOutputMixin:
    """Status emission (see module docstring)."""

    # Set by AIAgent; declared here so the mixin type-checks on its own.
    _print_fn: Callable[..., Any] | None

    def _safe_print(self, *args: Any, diagnostic: bool = False, **kwargs: Any) -> None:
        """Print that swallows broken pipes / closed stdout (headless stdout can vanish
        mid-session); routes through ``self._print_fn`` so the CLI can inject an ANSI-aware
        renderer."""
        try:
            (self._print_fn or print)(*args, **kwargs)
        except (OSError, ValueError):
            pass

    def _vprint(
        self, *args: Any, force: bool = False, diagnostic: bool = False, **kwargs: Any
    ) -> None:
        """Verbose print — suppressed after the main response; ``force=True`` bypasses it.
        ``suppress_status_output`` (``hermes chat -q``) wins."""
        if getattr(self, "suppress_status_output", False):
            return
        if force or not getattr(self, "_mute_post_response", False):
            self._safe_print(*args, **kwargs)
