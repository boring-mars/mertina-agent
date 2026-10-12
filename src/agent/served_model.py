"""Per-response *served model* capture for routing proxies (#54864).

A LiteLLM-style proxy answers with the configured alias in the body's ``model`` field and puts
the deployment it really routed to in a response header (``x-litellm-model-id``, else the
upstream ``x-litellm-model-api-base``). The OpenAI SDK's parsed objects drop headers, so the
capture rides an ``httpx`` response hook on the client Hermes builds for the agent; it stores
the header onto ``agent.last_served_model`` (``None`` when the response carried none, so a
value never outlives the request that produced it). Consumers: ``agent/turn_finalizer.py``
(result ``served_model`` / ``requested_model``) and the opt-in gateway footer field
``served_model`` (``gateway/runtime_footer.py``). Fail-open everywhere.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

SERVED_MODEL_HEADERS: tuple[str, ...] = ("x-litellm-model-id", "x-litellm-model-api-base")
_HOOK_MARK = "_hermes_served_model_hook"






def result_model_fields(agent: Any) -> dict[str, Optional[str]]:
    """``requested_model`` / ``served_model`` for the turn result: the proxy header when the
    last response carried one, else Hermes' own fallback route (primary → active model)."""
    served = getattr(agent, "last_served_model", None)
    requested = agent.model
    if not served and getattr(agent, "_fallback_activated", False):
        primary = str((getattr(agent, "_primary_runtime", None) or {}).get("model") or "").strip()
        if primary and primary != agent.model:
            requested, served = primary, agent.model
    return {"requested_model": requested, "served_model": served or None}
