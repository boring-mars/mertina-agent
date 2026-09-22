# Ported from hermes-agent utils.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Shared utility functions for hermes-agent."""

import json
from typing import Any
from urllib.parse import ParseResult, urlparse


def safe_json_loads(text: str, default: Any = None) -> Any:
    """Parse JSON, returning *default* on any parse error."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def _parse_base_url(base_url: str) -> ParseResult | None:
    """``urlparse`` that tolerates a bare ``host[:port][/path]`` (no scheme)."""
    raw = (base_url or "").strip()
    return urlparse(raw if "://" in raw else f"//{raw}") if raw else None


def _hostname_of(parsed: ParseResult | None) -> str:
    return (parsed.hostname or "").lower().rstrip(".") if parsed else ""


def base_url_hostname(base_url: str) -> str:
    """Lowercased hostname for a base URL, or ``""`` if absent.

    Compare exact hostnames against provider hosts instead of substring-matching the raw URL:
    ``https://api.openai.com.example/v1`` or ``https://proxy.test/api.openai.com/v1`` would
    otherwise pass as native endpoints and mis-route api_mode and auth.
    """
    return _hostname_of(_parse_base_url(base_url))


def base_url_host_matches(base_url: str, domain: str) -> bool:
    """True when the base URL's hostname is ``domain`` or a subdomain.

    Safer than ``domain in base_url`` (``evil.com/moonshot.ai`` / ``moonshot.ai.evil`` must not
    match). Accepts bare hosts, full URLs, and URLs with paths.
    """
    hostname = base_url_hostname(base_url)
    domain = (domain or "").strip().lower().rstrip(".")
    return bool(hostname and domain) and (hostname == domain or hostname.endswith("." + domain))
