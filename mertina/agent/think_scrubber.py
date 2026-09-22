# Ported from hermes-agent agent/think_scrubber.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Stateful scrubber for reasoning/thinking blocks in streamed assistant text.

The regex ``_strip_think_blocks`` is correct for a complete string but, run per-delta, erases an
opening ``<think>`` that arrives alone, so downstream state machines leak reasoning. This class
holds partial tags at delta boundaries until resolved; ``flush()`` releases held-back prose that
was not a tag; ``reset()`` at the top of each turn. An open tag only starts a block at a block
boundary (stream start / after a newline / whitespace-only line so far), so prose that *mentions*
``<think>`` is not suppressed; closed pairs are always suppressed (intentional).
"""

from __future__ import annotations

# The one list of model reasoning tag names. Every surface that hides reasoning (this scrubber,
# the CLI stream filter, the gateway stream filter, the final-response regex stripper) binds to
# these; a tag added here is covered everywhere. Consumers match case-insensitively, so the
# literal tags are lowercase.
THINK_TAG_NAMES: tuple[str, ...] = (
    "think",
    "thinking",
    "reasoning",
    "thought",
    "REASONING_SCRATCHPAD",
)
