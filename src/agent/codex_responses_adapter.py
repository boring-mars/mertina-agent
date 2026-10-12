"""Codex Responses API adapter: stateless format conversion and normalization for the
OpenAI Responses API (OpenAI Codex, xAI, GitHub Models and other compatible endpoints)."""


import hashlib
import re
import uuid

from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional, TypeGuard

















_TEXT_PART_TYPES = {"text", "input_text", "output_text"}
_IMAGE_PART_TYPES = {"image_url", "input_image"}


class ResponsesRouteFlags(NamedTuple):
    """Which special Responses-API route an agent is talking to. Single owner of the
    codex/xai/github predicates — every site must call :func:`classify_responses_route`.

    Every site that needs these flags (request kwargs build, preflight estimation, silent- reject hints)
    must call :func:`classify_responses_route` instead of re-implementing the string comparisons inline —
    inline copies drift (backend-identity class: #22548/#70893/#59561/#72468).
    """
    is_codex_backend: bool
    is_xai_responses: bool
    is_github_responses: bool


def classify_responses_route(agent: Any) -> ResponsesRouteFlags:
    """Classify the agent's Responses route from provider + base URL. Host checks are
    exact-host-or-subdomain, never substring (``evil.com/models.github.ai`` is not GitHub)."""
    from utils import base_url_hostname
    provider = getattr(agent, "provider", None)
    base_url = str(getattr(agent, "base_url", "") or "")
    hostname = str(getattr(agent, "_base_url_hostname", "") or "").lower() or base_url_hostname(base_url)
    lower = str(getattr(agent, "_base_url_lower", "") or base_url).lower()
    def _host_is(domain: str) -> bool:
        return hostname == domain or hostname.endswith("." + domain)
    return ResponsesRouteFlags(
        is_codex_backend=provider == "openai-codex" or (_host_is("chatgpt.com") and "/backend-api/codex" in lower),
        is_xai_responses=provider in {"xai", "xai-oauth"} or hostname == "api.x.ai",
        is_github_responses=_host_is("models.github.ai") or _host_is("githubcopilot.com"),
    )







def _nonempty_str(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value)


def _str_or_empty(value: Any) -> str:
    return "" if value is None else str(value)








def _part_type(part: Dict[str, Any]) -> str:
    return str(part.get("type") or "").strip().lower()











def _iter_content_parts(content: list) -> Iterator[tuple[str, Any]]:
    """Yield ``("text", str)`` / ``("image", part)`` for recognized chat parts."""
    for part in content:
        if isinstance(part, str) and part:
            yield "text", part
        elif isinstance(part, dict):
            ptype = _part_type(part)
            if ptype in _TEXT_PART_TYPES and _nonempty_str(part.get("text")):
                yield "text", part["text"]
            elif ptype in _IMAGE_PART_TYPES:
                yield "image", part






def _summarize_user_message_for_log(content: Any, *, sep: str = " ") -> str:
    """Flatten message content to plain text: text parts joined with ``sep`` (``" "`` for logs; ``"\\n"`` for memory
    providers feeding regexes), images → ``[N image(s)]`` marker, ``""`` for None, ``str(content)`` for other scalars."""
    if not isinstance(content, list):
        try:
            return _str_or_empty(content)
        except Exception:
            return ""
    parts = list(_iter_content_parts(content))
    text_bits = [payload for kind, payload in parts if kind == "text"]
    image_count = len(parts) - len(text_bits)
    note = f"[{image_count} image{'s' if image_count != 1 else ''}]" if image_count else ""
    return " ".join(bit for bit in (note, sep.join(text_bits).strip()) if bit)

def _split_responses_tool_id(raw_id: Any) -> tuple[Optional[str], Optional[str]]:
    """Split a stored tool id into (call_id, response_item_id)."""
    value = raw_id.strip() if isinstance(raw_id, str) else ""
    if "|" in value:
        call_id, response_item_id = value.split("|", 1)
        return call_id.strip() or None, response_item_id.strip() or None
    if not value:
        return None, None
    return (None, value) if value.startswith("fc_") else (value, None)

def _derive_responses_function_call_id(call_id: str, response_item_id: Optional[str] = None) -> str:
    """Build a valid Responses `function_call.id` (must start with `fc_`)."""
    if isinstance(response_item_id, str) and response_item_id.strip().startswith("fc_"):
        return response_item_id.strip()
    source = (call_id or "").strip()
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "", source)
    for candidate in (source, sanitized):
        if candidate.startswith("fc_") or (candidate.startswith("call_") and len(candidate) > len("call_")):
            return candidate if candidate.startswith("fc_") else f"fc_{candidate[len('call_'):]}"
    if sanitized:
        return f"fc_{sanitized[:48]}"
    seed = source or str(response_item_id or "") or uuid.uuid4().hex
    return f"fc_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:24]}"
