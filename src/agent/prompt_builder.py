"""Hermes prompt primitives retained by the Mertina v0.1 prompt layers."""

import contextvars

import json

import logging

import os

import queue

import sys

import threading

from collections import OrderedDict

from pathlib import Path

from typing import Any, Dict, Optional

from hermes_constants import (
    get_hermes_home, get_scratch_dir, get_skills_dir, is_wsl, reset_hermes_home_override, set_hermes_home_override,
)

from agent.model_metadata import CHARS_PER_TOKEN

from agent.runtime_cwd import resolve_agent_cwd

from tools.threat_patterns import scan_for_threats as _scan_for_threats

from utils import atomic_json_write, file_signature

logger = logging.getLogger(__name__)

_CONTEXT_FILE_READ_TIMEOUT_SECS = 5.0




DEFAULT_AGENT_IDENTITY = (
    # A behavior spec (sizing rule, named prohibitions, earned-depth escape hatch), not a trait list — trait
    # lists change nothing. Maintainer rule: models UNDER-explore by default; never re-add an exploration-thrift line.
    "You are Hermes Agent, built by Nous Research. Be direct: match the length of your reply to the weight of the ask "
    "— a one-line question gets a one-line answer, and finished work gets a short report of what changed, what's "
    "verified, and what's left, never a replay of the process. No filler (\"Great question,\" \"I'd be happy to\"), no "
    "restating the request back, no re-summarizing what you already said, no narrating tool calls the user can see. "
    "Plain claims over adjectives; when unsure, say so plainly. Agree because it's right, not because the user said "
    "it. Depth is earned — give it when the user asks for detail, teaches, or the stakes demand it, not by default."
)

PARALLEL_TOOL_CALL_GUIDANCE = (
    "# Parallel tool calls\n"
    "When you need several pieces of information that don't depend on each other, request them together in a "
    "single response instead of one tool call per turn. Independent reads, searches, web fetches, and "
    "read-only commands should be batched into the same assistant turn — the runtime executes independent "
    "calls concurrently, and batching avoids resending the whole conversation on every extra round-trip.\n"
    "Only serialize calls when a later call genuinely depends on an earlier call's result (e.g. you must "
    "read a file before you can patch it). When in doubt and the calls are independent, batch them."
)

STEER_MARKER_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered "
    "once at this position; not tool output and not a new delivery when replayed from conversation history]"
)

STEER_MARKER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"


STEER_DISPLAY_KIND = "steer"


DEVELOPER_ROLE_MODELS = ("gpt-5", "codex")


RUNTIME_ENVIRONMENT_HEADING = "# Hermes runtime environment"

RUNTIME_ENVIRONMENT_END = "<!-- End Hermes runtime environment -->"

CONTEXT_FILE_MAX_CHARS = 20_000

CONTEXT_TRUNCATE_HEAD_RATIO = 0.7

CONTEXT_TRUNCATE_TAIL_RATIO = 0.2

_CONTEXT_FILE_WINDOW_FRACTION = 0.06

_CONTEXT_FILE_DYNAMIC_CEILING = 500_000



_truncation_warnings: "contextvars.ContextVar[Optional[list]]" = contextvars.ContextVar(
    "context_file_truncation_warnings", default=None
)

_SKILLS_PROMPT_CACHE: OrderedDict[tuple, str] = OrderedDict()

_SKILLS_PROMPT_CACHE_LOCK = threading.Lock()

def _skills_prompt_snapshot_path() -> Path:
    return get_hermes_home() / ".skills_prompt_snapshot.json"

def clear_skills_system_prompt_cache(*, clear_snapshot: bool = False) -> None:
    """Drop the in-process skills prompt cache (and optionally the disk snapshot)."""
    with _SKILLS_PROMPT_CACHE_LOCK:
        _SKILLS_PROMPT_CACHE.clear()
    try:
        if clear_snapshot:
            _skills_prompt_snapshot_path().unlink(missing_ok=True)
    except OSError as e:
        logger.debug("Could not remove skills prompt snapshot: %s", e)
