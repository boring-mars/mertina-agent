# Ported from hermes-agent tools/environments/local_env_policy.py @ fbc4ea8b96
# Copyright (c) 2025 Nous Research. MIT License, see LICENSE.
# Partial: only the parts ported so far. Upstream order is kept.
"""Secret-scrub policy for Mertina child processes: pure data + predicates for which env
names are Mertina-managed credentials. The env *builders* applying it (``_make_run_env``,
``_sanitize_subprocess_env``, ``mertina_subprocess_env``) live in ``tools.environments.local``."""

# Prefix a caller uses in ``extra_env`` to force a blocklisted var through.
_MERTINA_PROVIDER_ENV_FORCE_PREFIX = "_MERTINA_FORCE_"


# Mertina-managed AWS *inference* credentials for ``auth_type="aws_sdk"`` (Bedrock):
# only the Bedrock bearer token, which no aws/terraform/boto3 toolchain uses. The
# general AWS chain stays inheritable on purpose — the local terminal is the user's
# trusted operator shell (SECURITY.md §3.2) and env_passthrough can never re-allow a
# blocklisted name (GHSA-rhgp-j443-p4rf), so blocking it would be unrecoverable.
_AWS_SDK_CREDENTIAL_ENV_VARS = frozenset({"AWS_BEARER_TOKEN_BEDROCK"})


_STATIC_PROVIDER_ENV_BLOCKLIST = frozenset(
    {
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "LLM_MODEL",
        "GOOGLE_API_KEY",
        # Path to a GCP service-account JSON, not a bare key, so OPTIONAL_ENV_VARS
        # marks it password=False and the registry loop skips it.
        "VERTEX_CREDENTIALS_PATH",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "PERPLEXITY_API_KEY",
        "COHERE_API_KEY",
        "FIREWORKS_API_KEY",
        "XAI_API_KEY",
        "HELICONE_API_KEY",
        "PARALLEL_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "TELEGRAM_HOME_CHANNEL",
        "TELEGRAM_HOME_CHANNEL_NAME",
        "DISCORD_HOME_CHANNEL",
        "DISCORD_HOME_CHANNEL_NAME",
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_AUTO_THREAD",
        "SLACK_HOME_CHANNEL",
        "SLACK_HOME_CHANNEL_NAME",
        "SLACK_ALLOWED_USERS",
        "WHATSAPP_ENABLED",
        "WHATSAPP_MODE",
        "WHATSAPP_ALLOWED_USERS",
        "SIGNAL_HTTP_URL",
        "SIGNAL_ACCOUNT",
        "SIGNAL_ALLOWED_USERS",
        "SIGNAL_GROUP_ALLOWED_USERS",
        "SIGNAL_HOME_CHANNEL",
        "SIGNAL_HOME_CHANNEL_NAME",
        "SIGNAL_IGNORE_STORIES",
        "HASS_TOKEN",
        "HASS_URL",
        "EMAIL_ADDRESS",
        "EMAIL_PASSWORD",
        "EMAIL_IMAP_HOST",
        "EMAIL_SMTP_HOST",
        "EMAIL_HOME_ADDRESS",
        "EMAIL_HOME_ADDRESS_NAME",
        "MERTINA_DASHBOARD_SESSION_TOKEN",
        "GATEWAY_ALLOWED_USERS",
        "GH_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_INSTALLATION_ID",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DAYTONA_API_KEY",
        "GATEWAY_RELAY_ID",
        "GATEWAY_RELAY_SECRET",
        "GATEWAY_RELAY_DELIVERY_KEY",
        "VERCEL_OIDC_TOKEN",
        "VERCEL_TOKEN",
        "VERCEL_PROJECT_ID",
        "VERCEL_TEAM_ID",
    }
)


def _build_provider_env_blocklist() -> frozenset[str]:
    """Derive the blocklist from provider, tool, and gateway config."""
    blocked: set[str] = set(_STATIC_PROVIDER_ENV_BLOCKLIST)
    # CLAUDE_CODE_OAUTH_TOKEN (via the anthropic registry entry) belongs to the user's Claude Code
    # install, not Mertina: stripping it made agent-spawned ``claude`` CLIs fall through to the
    # shared Keychain / ~/.claude store and, on auth failure, wipe it — logging the user out. BUZZ_*
    # is deliberately NOT discarded: this list feeds every scrub surface, so an import-time discard
    # would leak BUZZ_PRIVATE_KEY into non-terminal children; the Buzz carve-out is terminal-only
    # and context-gated (``_is_terminal_first_party_env``). It is set and owned by the user's Claude
    # Code install (subscription OAuth), not a Mertina-managed inference credential — Claude
    # subscription auth is not a working Mertina provider path. It arrives via the registry loop
    # above (anthropic api_key_env_vars), so remove it explicitly. See #55878.
    blocked.discard("CLAUDE_CODE_OAUTH_TOKEN")
    # BUZZ_* is deliberately NOT discarded here, even for Buzz-managed agents (BUZZ_MANAGED_AGENT
    # set by the buzz-acp harness). See #76243, #78026, #78065, #78511.
    return frozenset(blocked)


_MERTINA_PROVIDER_ENV_BLOCKLIST = _build_provider_env_blocklist()


def _is_provider_env_blocklisted(name: str) -> bool:
    """``name`` is a blocklisted provider/tool credential, matched the way the
    platform's environment resolves names: exact plus case-folded. On Windows
    the environment block is case-insensitive, so ``openai_api_key`` IS
    ``OPENAI_API_KEY``; consistent with ``_is_mertina_internal_secret``, which
    already folds (``key.upper()``)."""
    return (
        name in _MERTINA_PROVIDER_ENV_BLOCKLIST or name.upper() in _MERTINA_PROVIDER_ENV_BLOCKLIST
    )


def _is_mertina_internal_secret(key: str) -> bool:
    """True for Mertina-internal secrets injected under *dynamic* names the static
    blocklist cannot enumerate: ``AUXILIARY_<TASK>_API_KEY``/``_BASE_URL`` (per-task
    side-LLM credentials) and ``GATEWAY_RELAY_*_SECRET``/``_KEY``/``_TOKEN`` (relay
    auth; non-secret routing hints stay visible). Stripped on every spawn path
    regardless of env_passthrough registration or ``inherit_credentials``."""
    upper = key.upper()
    if upper.startswith("AUXILIARY_") and upper.endswith(("_API_KEY", "_BASE_URL")):
        return True
    return upper.startswith("GATEWAY_RELAY_") and upper.endswith(("_SECRET", "_KEY", "_TOKEN"))


def _plugin_terminal_env_strip_keys() -> frozenset[str]:
    """Credential env keys owned by plugin-registered terminal backends (Tier-1:
    stripped from every spawned subprocess). Computed at call time because plugins
    register after import; fail-soft to empty."""
    return frozenset()
