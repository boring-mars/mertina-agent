"""CLI presentation -- spinner, kawaii faces, tool preview formatting.

Pure display functions with no AIAgent dependency; used for CLI feedback.
"""

import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from utils import safe_json_loads
from agent.tool_result_classification import file_mutation_result_landed, is_guardrail_refusal

logger = logging.getLogger(__name__)



def _display_url(value: Any) -> str:
    """Extract a display-only URL without assuming model argument types."""
    value = (value.get("url") or value.get("href")) if isinstance(value, dict) else value
    return value.strip() if isinstance(value, str) else ""






def _get_skin():
    """Active skin config, or None when unavailable (lazy import avoids cycles)."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        return get_active_skin()
    except Exception:
        return None














_tool_preview_max_len: int = 0  # 0 = unlimited
_friendly_tool_labels: bool = True








def get_skin_tool_prefix() -> str:
    """Get tool output prefix character from active skin."""
    skin = _get_skin()
    return skin.tool_prefix if skin else "┊"


def get_tool_emoji(tool_name: str, default: str = "⚡") -> str:
    """Display emoji for a tool: skin ``tool_emojis`` override, then registry, then *default*."""
    skin = _get_skin()
    override = skin.tool_emojis.get(tool_name) if skin and skin.tool_emojis else None
    if override:
        return override
    try:
        from tools.registry import registry
        return registry.get_emoji(tool_name, default="") or default
    except Exception:
        return default



def _oneline(text: str) -> str:
    """Collapse whitespace (including newlines) to single spaces."""
    return " ".join(text.split())


def _tail_trunc(text: str, limit: int | None) -> str:
    """Tail-truncate to ``limit`` chars with ``...`` (0/None = unlimited). The result never
    exceeds ``limit``: for 1-3 the ellipsis itself is clipped (``text[:limit - 3]`` would go
    negative and hand back almost the whole string, #9439)."""
    if not limit or limit <= 0 or len(text) <= limit:
        return text
    return "." * limit if limit <= 3 else text[:limit - 3] + "..."

































def redact_tool_args_for_display(tool_name: str, args: dict | None) -> dict | None:
    """Return a copy of tool args safe for logs/progress UI (masks ``browser_type`` secrets)."""
    if not isinstance(args, dict):
        return args
    return args






_PRIMARY_ARGS = {"web_search": "query"}
_FALLBACK_PREVIEW_KEYS = ("query", "text", "command", "path", "name", "prompt", "code", "goal")






















_PREVIEW_BUILDERS = {}


def build_tool_preview(tool_name: str, args: dict, max_len: int | None = None) -> str | None:
    """Build a short preview of a tool call's primary argument for display.

    *max_len* ``None`` defers to the global ``_tool_preview_max_len``; ``0`` means unlimited.
    """
    if max_len is None:
        max_len = _tool_preview_max_len
    if not args:
        return None
    args = redact_tool_args_for_display(tool_name, args) or args
    builder = _PREVIEW_BUILDERS.get(tool_name)
    if builder is not None:
        return builder(args, max_len)
    key = _PRIMARY_ARGS.get(tool_name) or next((k for k in _FALLBACK_PREVIEW_KEYS if k in args), None)
    if not key or key not in args:
        return None
    value = args[key]
    preview = _oneline(str((value[0] if value else "") if isinstance(value, list) else value))
    return _tail_trunc(preview, max_len) if preview else None





_TOOL_VERBS: dict[str, str] = {"web_search": "Searching the web"}
_TOOL_VERBS_NO_PREVIEW: frozenset[str] = frozenset()
_TOOL_VERBS_FOR_CONNECTOR: frozenset[str] = frozenset({"web_search"})

def get_tool_verb(tool_name: str) -> str | None:
    """Friendly verb for a built-in tool, or None (labels disabled / no curated verb);
    callers compose ``f"{verb}{tool_verb_connector(tool)}{preview}"`` themselves."""
    return _TOOL_VERBS.get(tool_name) if _friendly_tool_labels else None


def tool_verb_connector(tool_name: str) -> str:
    """Return the connector between a verb and its preview (" for " or " ")."""
    return " for " if tool_name in _TOOL_VERBS_FOR_CONNECTOR else " "







def build_tool_label(tool_name: str, args: dict, max_len: int | None = None) -> str | None:
    """Human-phrased label ("Searching the web for ...") for curated built-ins; other
    tools (or labels disabled) get the raw preview, so it is a drop-in for build_tool_preview."""
    verb = get_tool_verb(tool_name)
    if verb and tool_name in _TOOL_VERBS_NO_PREVIEW:
        return verb
    preview = build_tool_preview(tool_name, args, max_len=max_len)
    if not verb:
        return preview
    return f"{verb}{tool_verb_connector(tool_name)}{preview}" if preview else verb



































class KawaiiSpinner:
    """Animated spinner with kawaii faces for CLI feedback during tool execution."""

    SPINNERS = {
        'dots': list('⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'), 'bounce': list('⠁⠂⠄⡀⢀⠠⠐⠈'), 'grow': list('▁▂▃▄▅▆▇█▇▆▅▄▃▂'),
        'arrows': list('←↖↑↗→↘↓↙'), 'star': list('✶✷✸✹✺✹✸✷'), 'moon': list('🌑🌒🌓🌔🌕🌖🌗🌘'),
        'pulse': list('◜◠◝◞◡◟'), 'brain': list('🧠💭💡✨💫🌟💡💭'), 'sparkle': list('⁺˚*✧✦✧*˚'),
    }
    KAWAII_WAITING = [
        "(｡◕‿◕｡)", "(◕‿◕✿)", "٩(◕‿◕｡)۶", "(✿◠‿◠)", "( ˘▽˘)っ", "♪(´ε` )", "(◕ᴗ◕✿)", "ヾ(＾∇＾)", "(≧◡≦)", "(★ω★)",
    ]
    KAWAII_THINKING = [
        "(｡•́︿•̀｡)", "(◔_◔)", "(¬‿¬)", "( •_•)>⌐■-■", "(⌐■_■)", "(´･_･`)", "◉_◉", "(°ロ°)", "( ˘⌣˘)♡", "ヽ(>∀<☆)☆",
        "٩(๑❛ᴗ❛๑)۶", "(⊙_⊙)", "(¬_¬)", "( ͡° ͜ʖ ͡°)", "ಠ_ಠ",
    ]
    THINKING_VERBS = [
        "pondering", "contemplating", "musing", "cogitating", "ruminating", "deliberating", "mulling",
        "reflecting", "processing", "reasoning", "analyzing", "computing", "synthesizing", "formulating",
        "brainstorming",
    ]

    @staticmethod
    def _skin_spinner_list(key: str, fallback: list) -> list:
        """Return the active skin's ``spinner[key]`` list, or *fallback* when absent/empty."""
        try:
            skin = _get_skin()
            return (skin.spinner.get(key, []) if skin else None) or fallback
        except Exception:
            return fallback

    get_waiting_faces = classmethod(lambda cls: cls._skin_spinner_list("waiting_faces", cls.KAWAII_WAITING))
    get_thinking_faces = classmethod(lambda cls: cls._skin_spinner_list("thinking_faces", cls.KAWAII_THINKING))
    get_thinking_verbs = classmethod(lambda cls: cls._skin_spinner_list("thinking_verbs", cls.THINKING_VERBS))

    def __init__(self, message: str = "", spinner_type: str = 'dots', print_fn=None):
        self.message = message
        self.spinner_frames = self.SPINNERS.get(spinner_type, self.SPINNERS['dots'])
        self.running = False
        self.thread = self.start_time = None
        self.frame_idx = self.last_line_len = 0
        self._print_fn = print_fn  # when set, bypasses self._out so silenced agents stay silent
        self._out = sys.stdout  # captured NOW, before any child redirect_stdout(devnull) replaces it

    def _write(self, text: str, end: str = '\n', flush: bool = False):
        """Write via print_fn when supplied, else to the stdout captured at creation."""
        if self._print_fn is not None:
            try:
                self._print_fn(text)
            except Exception:
                pass
            return
        try:
            self._out.write(text + end)
            if flush:
                self._out.flush()
        except (ValueError, OSError):
            pass




    def start(self):
        if self.running:
            return
        self.running, self.start_time = True, time.time()
        self.thread = threading.Thread(target=self._animate, daemon=True)
        self.thread.start()


    def _clear_line_blanks(self) -> str:
        return ' ' * max(self.last_line_len + 5, 40)  # spaces, not \033[K: garbles under patch_stdout


    def stop(self, final_message: str = None):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        is_tty = self._is_tty
        if is_tty:
            self._write(f"\r{self._clear_line_blanks()}\r", end='', flush=True)
        if final_message:
            elapsed = f" ({time.time() - self.start_time:.1f}s)" if self.start_time else ""
            self._write(f"  {final_message}" if is_tty else f"  [done] {final_message}{elapsed}", flush=True)





_ERROR_SUFFIX_MAX_LEN = 48
_DEGRADED_SUFFIX_MAX_LEN = 200


def _trim_error(msg: str) -> str:
    """Shrink an error message for inline display (long 'File not found' paths -> filename)."""
    msg = msg.strip()
    if "File not found:" in msg:
        tail = msg.partition("File not found:")[2].strip()
        if "/" in tail:
            msg = f"File not found: {tail.rsplit('/', 1)[-1]}"
    return _tail_trunc(msg, _ERROR_SUFFIX_MAX_LEN)


def _degraded_suffix(data: dict) -> str:
    """`` [<reason> — <retry_hint>]`` for a ``status: degraded`` terminal result (hint omitted when empty)."""
    reason = str(data.get("reason") or data.get("error") or "terminal backend unavailable").strip()
    hint = str(data.get("retry_hint") or "").strip()
    text = f"{reason} — {hint}" if hint else reason
    return f" [{_tail_trunc(text, _DEGRADED_SUFFIX_MAX_LEN)}]"


def _detect_tool_failure(tool_name: str, result: Any) -> tuple[bool, str]:
    """Return ``(is_failure, suffix)`` for a tool result, e.g. ``(True, " [exit 1]")``."""
    if result is None or file_mutation_result_landed(tool_name, result):
        return False, ""
    data = result if isinstance(result, dict) else safe_json_loads(result)
    # A harness REFUSAL of a redundant call (repeated identical read/search) is not a
    # failed call. This is the ``failed`` the executor hands the loop guardrail, so
    # counting it would escalate refusals into ``repeated_exact_failure_block``.
    if is_guardrail_refusal(data):
        return False, ""

    # A denied/timed-out approval carries one human sentence; show it instead of the model-facing
    # "BLOCKED: ... Do NOT retry" text (which stays in the JSON for the model).
    if isinstance(data, dict) and data.get("user_summary"):
        return True, f" [{_tail_trunc(str(data['user_summary']), _DEGRADED_SUFFIX_MAX_LEN)}]"

    # Terminal: non-zero exit code is the canonical failure signal.
    if tool_name == "terminal":
        exit_code = data.get("exit_code") if isinstance(data, dict) else None
        if exit_code is None or exit_code == 0:
            return False, ""
        if data.get("status") == "degraded":
            return True, _degraded_suffix(data)
        err_msg = data.get("error")
        return True, f" [{_trim_error(str(err_msg))}]" if err_msg else f" [exit {exit_code}]"

    if isinstance(data, dict):
        failed = data.get("success") is False
        # Memory: distinguish "store full" from real errors.
        if tool_name == "memory" and failed and "exceed the limit" in data.get("error", ""):
            return True, " [full]"
        err = data.get("error") or data.get("message")
        if err and (failed or "error" in data):
            return True, f" [{_trim_error(str(err))}]"
    # Multimodal results (dicts) are successes; failures arrive as JSON-encoded strings.
    if isinstance(result, str) and (
        '"error"' in result[:500].lower() or '"failed"' in result[:500].lower() or result.startswith("Error")
    ):
        return True, " [error]"
    return False, ""




def _cute_trunc(s) -> str:
    """Tail-truncate to the configured preview cap (0 = unlimited)."""
    return _tail_trunc(str(s), _tool_preview_max_len)























_CUTE_LINES = {
    "web_search": lambda a, r: f"┊ 🔍 search    {_cute_trunc(a.get('query', ''))}",
}


def _get_cute_tool_message(tool_name: str, args: dict, duration: float, result: str | None = None) -> str:
    """Tool completion line for CLI quiet mode: ``| {emoji} {verb:9} {detail}  {duration}``, plus a
    failure suffix from :func:`_detect_tool_failure`; the leading ``┊`` becomes the skin's tool prefix."""
    args = redact_tool_args_for_display(tool_name, args) or args
    is_failure, failure_suffix = _detect_tool_failure(tool_name, result)
    render = _CUTE_LINES.get(tool_name)
    body = render(args, result) if render else f"┊ ⚡ {tool_name[:9]:9} {_cute_trunc(build_tool_preview(tool_name, args) or '')}"
    line = f"{body}  {duration:.1f}s".replace("┊", get_skin_tool_prefix(), 1)
    return f"{line}{failure_suffix}" if is_failure else line


def get_cute_tool_message(tool_name: str, args: dict, duration: float, result: str | None = None) -> str:
    """Render a completion label without letting cosmetic failures escape."""
    try:
        return _get_cute_tool_message(tool_name, args, duration, result=result)
    except Exception as exc:  # noqa: BLE001 — display must never abort a turn
        logger.debug("Tool completion label failed for %s: %s", tool_name, exc)
        safe_name = tool_name[:9] if isinstance(tool_name, str) and tool_name else "tool"
        safe_duration = f"{duration:.1f}s" if isinstance(duration, (int, float)) else "done"
        return f"┊ ⚡ {safe_name:9} completed  {safe_duration}"


