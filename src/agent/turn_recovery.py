"""Response validation retained from Hermes for the v0.1 Chat Completions loop."""

from __future__ import annotations

import logging
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)

def validate_response_shape(agent: Any, response: Any) -> Tuple[bool, List[str]]:
    """Validate the raw provider response via the transport; ``(response_invalid,
    error_details)``. A Codex ``failed``/``cancelled`` status (e.g. quota exhaustion) is
    invalid so the fallback chain triggers; an empty Codex ``output`` with non-empty
    ``output_text`` is deferred to normalization."""
    if agent._get_transport().validate_response(response):
        return False, []
    if response is None:
        return True, ["response is None"]
    if agent.api_mode == "anthropic_messages":
        detail = "response.content invalid (not a non-empty list)"
    elif agent.api_mode == "bedrock_converse":
        detail = "Bedrock response invalid (no output or choices)"
    elif not hasattr(response, 'choices'):
        detail = "response has no 'choices' attribute"
    elif response.choices is None:
        detail = "response.choices is None"
    else:
        detail = "response.choices is empty"
    return True, [detail]
