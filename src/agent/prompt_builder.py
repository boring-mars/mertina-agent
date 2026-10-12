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

def _get_context_file_read_timeout() -> float:
    """``context_file_read_timeout`` from config.yaml, else the 5s default."""
    val = _config_readonly("context_file_read_timeout").get("context_file_read_timeout")
    if isinstance(val, (int, float)) and val > 0:
        return float(val)
    return _CONTEXT_FILE_READ_TIMEOUT_SECS

def _read_text_with_timeout(path: Path, timeout: Optional[float] = None) -> Optional[str]:
    """``path.read_text()`` on a daemon thread so a slow file can't stall startup.

    Returns the text, or ``None`` after *timeout* seconds (logged at WARNING;
    the orphaned reader thread finishes on its own). Read errors propagate to
    the caller exactly as a direct ``read_text`` would, so existing
    ``try/except`` handling at each site is unchanged.
    """
    if timeout is None:
        timeout = _get_context_file_read_timeout()
    result: "queue.Queue[tuple[bool, object]]" = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            result.put((True, path.read_text(encoding="utf-8")))
        except Exception as exc:  # re-raised on the caller thread
            result.put((False, exc))

    threading.Thread(target=_reader, daemon=True, name=f"context-read:{path.name}").start()
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty:
        logger.warning("Context file %s read timed out after %.1fs; skipping", path, timeout)
        return None
    if ok:
        return value  # type: ignore[return-value]
    raise value

def _scan_context_content(content: str, filename: str, *, user_authored: bool = False) -> str:
    """Scan a context file (AGENTS.md, .cursorrules, SOUL.md) for injection; matches are BLOCKED.

    "context" scope only (strict-scope SSH-backdoor/persistence/exfil patterns are too aggressive for a
    cloned repo's docs); blocking, not warning, because the file would otherwise enter the prompt verbatim.

    *user_authored* (SOUL.md in the user's own HERMES_HOME): a hit is WARNED and the file still loads.
    SOUL.md sits in the same trust class as config.yaml — file-tool writes to it go through the
    protected-instruction approval gate (``tools/file_tools_write_guards.py``) and project checkouts never
    supply it — so a user who *documents* "ignore previous instructions" in their security guidance
    must not lose their whole identity file to a one-line log entry (#112570). Project-dir files
    (repo AGENTS.md / .cursorrules / .hermes.md) arrive with the checkout and keep blocking, and so does
    a SOUL.md owned by a profile distribution (``hermes profile install <git-url>`` copies it in unscanned;
    ``load_soul_md`` passes ``user_authored=False`` when ``distribution.yaml`` owns the file).
    """
    # A leading UTF-8 BOM is a Windows-editor artifact, not an injection.
    if content.startswith("\ufeff"):
        content = content[1:]
    findings = _scan_for_threats(content, scope="context")
    if not findings:
        return content
    if user_authored:
        logger.warning("Context file %s matched injection pattern(s) %s; loaded anyway because it is the "
                       "user's own file in HERMES_HOME — review it if you did not write that text",
                       filename, ", ".join(findings))
        return content
    logger.warning("Context file %s blocked: %s", filename, ", ".join(findings))
    return f"[BLOCKED: {filename} contained potential prompt injection ({', '.join(findings)}). Content not loaded.]"

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

def format_steer_marker(steer_text: str) -> str:
    """Wrap a mid-turn steer in the self-describing marker (see note above)."""
    return f"\n\n{STEER_MARKER_OPEN}\n{steer_text}\n{STEER_MARKER_CLOSE}"

STEER_DISPLAY_KIND = "steer"

def steer_user_row(steer_text: str) -> Dict[str, Any]:
    """The standalone ``role:user`` row a mid-turn /steer is delivered as (after the newest tool
    result). Its own row — never smeared onto the already-persisted tool row, which append-only
    persistence would leave divergent from the live request — and typed so the alternation repair
    never merges the next real prompt into it and history renderers can label it."""
    return {"role": "user", "content": format_steer_marker(steer_text).lstrip(),
            "display_kind": STEER_DISPLAY_KIND}

DEVELOPER_ROLE_MODELS = ("gpt-5", "codex")

def _config_readonly(what: str) -> dict:
    """config.yaml as a dict, or {} when unreadable (logged at debug with *what* for context)."""
    try:
        from hermes_cli.config import load_config_readonly
        return load_config_readonly()
    except Exception as e:
        logger.debug("Could not read %s from config: %s", what, e)
        return {}

RUNTIME_ENVIRONMENT_HEADING = "# Hermes runtime environment"

RUNTIME_ENVIRONMENT_END = "<!-- End Hermes runtime environment -->"

CONTEXT_FILE_MAX_CHARS = 20_000

CONTEXT_TRUNCATE_HEAD_RATIO = 0.7

CONTEXT_TRUNCATE_TAIL_RATIO = 0.2

_CONTEXT_FILE_WINDOW_FRACTION = 0.06

_CONTEXT_FILE_DYNAMIC_CEILING = 500_000

def _dynamic_context_file_max_chars(context_length: Optional[int]) -> int:
    """Char cap from the model's window, clamped to [20K floor, 500K ceiling]; flat default when unknown."""
    if not isinstance(context_length, int) or context_length <= 0:
        return CONTEXT_FILE_MAX_CHARS
    budget = int(context_length * CHARS_PER_TOKEN * _CONTEXT_FILE_WINDOW_FRACTION)
    return max(CONTEXT_FILE_MAX_CHARS, min(budget, _CONTEXT_FILE_DYNAMIC_CEILING))

def _get_context_file_max_chars(context_length: Optional[int] = None) -> int:
    """Context-file truncation limit: explicit config.yaml ``context_file_max_chars`` wins, else the dynamic cap."""
    val = _config_readonly("context_file_max_chars").get("context_file_max_chars")
    return int(val) if isinstance(val, (int, float)) and val > 0 else _dynamic_context_file_max_chars(context_length)

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

def _truncate_content(
    content: str, filename: str, max_chars: Optional[int] = None, context_length: Optional[int] = None,
    read_path: Optional[str] = None, queue_warning: bool = True,
) -> str:
    """Head/tail truncation with a marker in the middle; ``read_path`` (default ``filename``) is what the
    agent is told to ``read_file`` to recover the full content. ``queue_warning=False`` is for bounded
    previews (subdirectory hints) whose fixed cap no config key or model raises: the truncation is logged
    with the marker as the only disclosure, never queued for the chat status line."""
    if max_chars is None:
        max_chars = _get_context_file_max_chars(context_length)
    if len(content) <= max_chars:
        return content
    remedy = (
        "trim the file, pin a larger context_file_max_chars, or use a larger-context model!" if queue_warning
        else f"the full file stays readable with read_file: {read_path or filename}"
    )
    msg = f"⚠️  Context file {filename} TRUNCATED: {len(content)} chars exceeds limit of {max_chars} — {remedy}"
    logger.warning(msg)
    if queue_warning:
        if (warnings := _truncation_warnings.get()) is None:
            _truncation_warnings.set(warnings := [])
        warnings.append(msg)
    head_chars = int(max_chars * CONTEXT_TRUNCATE_HEAD_RATIO)
    tail_chars = int(max_chars * CONTEXT_TRUNCATE_TAIL_RATIO)
    marker = (
        f"\n\n[...truncated {filename}: kept {head_chars}+{tail_chars} of {len(content)} chars. The middle is "
        f"omitted — if you need the full instructions, read the complete file with the read_file tool: "
        f"{read_path or filename}]\n\n"
    )
    return content[:head_chars] + marker + content[-tail_chars:]
