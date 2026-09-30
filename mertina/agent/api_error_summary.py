# Ported from hermes-agent agent/api_error_summary.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
"""Provider API error summarising for ``AIAgent``.

Structured-detail coercion and one-line summaries of provider error payloads.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""

import json
import re
from typing import Any

# Offline DNS failures are wrapped in a generic "Connection error" by SDKs — inspect the chain.
_NETWORK_RESOLUTION_MARKERS = (
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided, or not known",
    "getaddrinfo failed",
    "no address associated with hostname",
    "network is unreachable",
)
_ERROR_DETAIL_KEYS = ("message", "detail", "error", "code", "type")


def _http_prefix(error: Exception) -> str:
    status_code = getattr(error, "status_code", None)
    return f"HTTP {status_code}: " if status_code else ""


class ApiErrorSummaryMixin:
    """Provider error -> user/log-safe summary (see module docstring)."""

    @staticmethod
    def _coerce_api_error_detail(value: Any) -> str:
        """Return a display-safe string for structured provider error fields."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in _ERROR_DETAIL_KEYS:
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested
            for key in _ERROR_DETAIL_KEYS:
                if key in value:
                    nested_detail = ApiErrorSummaryMixin._coerce_api_error_detail(value[key])
                    if nested_detail:
                        return nested_detail
            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            except TypeError:
                return str(value)
        if isinstance(value, (list, tuple)):
            parts = [ApiErrorSummaryMixin._coerce_api_error_detail(item) for item in value]
            return "; ".join(part for part in parts if part)
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _summarize_api_error(error: Exception) -> str:
        """Extract a human-readable one-liner from an API error.

        Cloudflare HTML pages → ``<title>``; network/DNS failures (even SDK-wrapped) → offline
        hint; else truncated str(error).
        """
        raw = str(error)

        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if any(marker in str(current).lower() for marker in _NETWORK_RESOLUTION_MARKERS):
                return (
                    "Mertina can't reach the model provider. You may be offline. "
                    "Check your internet connection and try again."
                )
            current = current.__cause__ or current.__context__

        prefix = _http_prefix(error)
        # Cloudflare / proxy HTML pages: grab the <title> (and Ray ID) for a clean summary
        if "<!DOCTYPE" in raw or "<html" in raw:
            m = re.search(r"<title[^>]*>([^<]+)</title>", raw, re.IGNORECASE)
            title = m.group(1).strip() if m else "HTML error page (title not found)"
            ray = re.search(r"Cloudflare Ray ID:\s*<strong[^>]*>([^<]+)</strong>", raw)
            parts = [prefix[:-2]] if prefix else []
            parts.append(title)
            if ray:
                parts.append(f"Ray {ray.group(1).strip()}")
            return " — ".join(parts)

        # JSON body errors from OpenAI/Anthropic SDKs
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            msg = (
                body.get("error", {}).get("message")
                if isinstance(body.get("error"), dict)
                else body.get("message")
            )
            if msg:
                msg = ApiErrorSummaryMixin._coerce_api_error_detail(msg)
                return f"{prefix}{msg[:300]}"

        # Fallback: truncate the raw string but give more room than 200 chars
        return f"{prefix}{raw[:500]}"

    def _clean_error_message(self, error_msg: str) -> str:
        """Clean up error messages for user display, removing HTML content and truncating."""
        if not error_msg:
            return "Unknown error"
        # HTML content is common with CloudFlare and gateway error pages
        if error_msg.strip().startswith("<!DOCTYPE html") or "<html" in error_msg:
            return "Service temporarily unavailable (HTML error page returned)"
        cleaned = " ".join(error_msg.split())
        if len(cleaned) > 150:
            cleaned = cleaned[:150] + "..."
        return cleaned
