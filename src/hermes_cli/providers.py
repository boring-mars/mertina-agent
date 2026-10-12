"""Single source of truth for provider identity in Hermes Agent."""


from typing import Any, Dict, List, Optional, Tuple

from utils import base_url_host_matches, base_url_hostname













_ALIAS_GROUPS: Dict[str, Tuple[str, ...]] = {
    "openrouter": ("openai",), "zai": ("glm", "z-ai", "z.ai", "zhipu"), "xai": ("x-ai", "x.ai", "grok"),
    "xai-oauth": ("grok-oauth", "xai-oauth", "x-ai-oauth", "xai-grok-oauth"),
    "nvidia": ("nim", "nvidia-nim", "build-nvidia", "nemotron"),
    "kimi-for-coding": ("kimi", "kimi-coding", "kimi-coding-cn", "moonshot"),
    "stepfun": ("step", "stepfun-coding-plan"), "minimax-cn": ("minimax-china", "minimax_cn"),
    "anthropic": ("claude", "claude-code"), "github-copilot": ("copilot", "github"),
    "copilot-acp": ("github-copilot-acp",), "openai-codex": ("chatgpt", "chatgpt-codex"),
    "vercel": ("ai-gateway", "aigateway", "vercel-ai-gateway"),
    "opencode": ("opencode-zen", "zen"), "opencode-go": ("go", "opencode-go-sub"), "kilo": ("kilocode", "kilo-code", "kilo-gateway"),
    "deepseek": ("deep-seek",), "alibaba": ("dashscope", "aliyun", "qwen", "alibaba-cloud"),
    "alibaba-coding-plan": ("alibaba_coding", "alibaba-coding", "alibaba_coding_plan"),
    "huggingface": ("hf", "hugging-face", "huggingface-hub"), "novita": ("novita-ai", "novitaai"),
    "xiaomi": ("mimo", "xiaomi-mimo"), "tencent-tokenhub": ("tencent", "tokenhub", "tencent-cloud", "tencentmaas"),
    "tencent-tokenplan": ("tokenplan", "tencent-lkeap"),
    "bedrock": ("aws", "aws-bedrock", "amazon-bedrock", "amazon"), "arcee": ("arcee-ai", "arceeai"),
    "gmi": ("gmi-cloud", "gmicloud"), "fireworks": ("fireworks-ai", "fw"), "upstage": ("solar",),
    "actual": ("actual-computer", "actualcomputer", "aci"),
    "nebius-token-factory": ("nebius", "nebius-tokenfactory", "nebius-tf", "token-factory", "tokenfactory"),
    "lmstudio": ("lmstudio", "lm-studio", "lm_studio"), "custom": ("ollama",),
    "local": ("vllm", "llamacpp", "llama.cpp", "llama-cpp"),
}
ALIASES: Dict[str, str] = {alias: canon for canon, aliases in _ALIAS_GROUPS.items() for alias in aliases}









def normalize_provider(name: str) -> str:
    """Resolve aliases and normalise casing to a canonical provider id."""
    key = name.strip().lower()
    return ALIASES.get(key, key)


def is_actual_route(provider: str = "", base_url: str = "") -> bool:
    """Identify Actual by provider/alias or its hosted endpoint, including custom routes."""
    return (
        normalize_provider(provider or "") == "actual"
        or base_url_hostname(base_url) == "api.actual.inc"
    )


















def is_official_openai_host(base_url: str) -> bool:
    """True when *base_url* points at OpenAI's official API host family. Hostname-parsed matching
    only — never substring — so lookalike hosts (``api.openai.com.attacker.test``) and path-segment
    spoofs (``proxy.test/api.openai.com/v1``) are rejected; a genuine ``*.api.openai.com``
    subdomain requires control of openai.com DNS.

    A genuine ``*.api.openai.com`` subdomain requires control of openai.com DNS, so the dot-suffix match
    does not reopen the #32243 spoofing hole. Delegates to ``utils.base_url_host_matches``, which owns the
    exact-or-dot-suffix hostname contract (userinfo/port stripped, lowercased, trailing dot removed) — one
    implementation, not two.
    """
    return base_url_host_matches(base_url, "api.openai.com")


_RESPONSES_NATIVE_HOSTS: frozenset[str] = frozenset({"api.meta.ai", "api.router.com"})


def host_mandated_api_mode(base_url: str = "") -> Optional[str]:
    """Return the wire protocol a specific endpoint *requires*, or None. Some hosts accept exactly
    one API mode (api.openai.com 400s chat/completions for reasoning models with tools); these are
    *mandatory*: a session carrying a stale api_mode (a /model switch that kept the previous
    provider's ``chat_completions``) must be overridden, not merely filled in when empty.
    Exact-hostname matching only — never substring — so lookalike hosts and path-segment spoofs are
    not treated as the real endpoint."""
    if not base_url:
        return None
    url_lower = base_url.rstrip("/").lower()
    hostname = base_url_hostname(base_url)
    if hostname == "api.actual.inc":
        return "chat_completions"
    # Exact-hostname matching only — never bare substring — so lookalike hosts
    # (api.openai.com.attacker.test) and path-segment spoofs (proxy.test/api.openai.com/v1) are NOT treated
    # as the real endpoint. (#32243)
    if hostname == "api.kimi.com" and "/coding" in url_lower:
        return "anthropic_messages"
    if hostname == "api.anthropic.com" or url_lower.endswith("/anthropic"):
        return "anthropic_messages"
    # Official OpenAI host family (canonical + us./eu. data-residency hosts) mandates Responses;
    # the shared predicate keeps this in lockstep with catalog filtering and listing authority.
    if is_official_openai_host(base_url) or hostname in _RESPONSES_NATIVE_HOSTS:
        # Ramp Router (api.router.com) is Responses-native: reasoning-effort validation, reasoning
        # summaries, and prompt caching live on /v1/responses, and /v1/chat/completions is only a minimal
        # compatibility shim (docs.router.com/api/endpoint). Exact-hostname match per #32243.
        return "codex_responses"
    if hostname.startswith("bedrock-runtime.") and base_url_host_matches(base_url, "amazonaws.com"):
        return "bedrock_converse"
    return None


def nous_api_mode(model: str = "") -> str:
    """Wire protocol for a Nous Portal model. Portal serves its ``anthropic/*`` catalog on a native
    Messages route alongside OpenAI-compatible chat/completions for everything else.

    ``anthropic/*`` rides chat/completions by default for now (``nous.anthropic_wire``). Measured
    2026-09-06, 20 concurrent sessions x 6 tool calls on Fable 5.1, same account and hour: the
    native route re-wrote the previous turn on 14-20% of consecutive calls (4 runs; the cache read
    stopped at the prior breakpoint with byte-identical prefixes), chat/completions 0 of 320 pairs.
    That is 15-20% of a fan-out's cache-write bill. The cause is inside the portal's native route
    (NousResearch/api#227 carries the diagnostics); flip the default back to ``native`` when it is
    fixed. Cost of ``chat``: prior-turn thinking travels as OpenAI-style reasoning fields instead of
    signed native blocks, and cache_control scopes are translated by the portal's adapter.
    Empty/unknown model defaults to ``chat_completions`` (the historical Nous transport)."""
    if str(model or "").strip().lower().startswith("anthropic/"):
        # ``auto`` starts on chat too: it is safe on every upstream, and ``agent/nous_wire.py``
        # promotes the session to native from the first response when the upstream allows it.
        return "anthropic_messages" if _nous_anthropic_wire() == "native" else "chat_completions"
    return "chat_completions"


def _nous_anthropic_wire() -> str:
    """``nous.anthropic_wire``: ``"chat"`` (default), ``"native"``, or ``"auto"`` (chat, then per-session
    promotion decided from the first response; see ``agent/nous_wire.py``). Anything else reads as ``chat``."""
    try:
        from hermes_cli.config import load_config_readonly
        value = str(((load_config_readonly().get("nous") or {}).get("anthropic_wire")) or "chat").strip().lower()
    except Exception:
        return "chat"
    return value if value in ("native", "auto") else "chat"























