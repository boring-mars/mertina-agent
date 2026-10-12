"""OpenAI-compatible facade over Google AI Studio's native Gemini API: the ``gemini``
provider keeps ``api_mode='chat_completions'`` so the agent loop stays OpenAI-shaped,
and this shim converts ``messages[]``/``tools[]`` into ``models/{model}:generateContent``
payloads and responses back. Google's OpenAI-compat endpoint is brittle for the
multi-turn tool loop (auth churn, tool-call replay, thought signatures); native is canonical."""

from __future__ import annotations

def is_vertex_express_base_url(base_url: str) -> bool:
    """An aiplatform host without a project path — the express surface. The OAuth Vertex provider's
    ``…/projects/{p}/locations/{r}/endpoints/openapi`` base is OpenAI-compatible and must stay off
    the native adapter."""
    normalized = str(base_url or "").strip().lower()
    return "aiplatform.googleapis.com" in normalized and "/projects/" not in normalized

def is_native_gemini_base_url(base_url: str) -> bool:
    """True when the endpoint speaks Gemini's native REST API (not ``/openai``)."""
    normalized = str(base_url or "").strip().rstrip("/").lower()
    if normalized.endswith("/openai"):
        return False
    return "generativelanguage.googleapis.com" in normalized or is_vertex_express_base_url(normalized)

