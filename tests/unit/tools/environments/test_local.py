"""The credential scrub applied to a spawned subprocess's environment."""

from mertina.tools.environments.local import _sanitize_subprocess_env
from mertina.tools.environments.local_env_policy import (
    _is_mertina_internal_secret,
    _is_provider_env_blocklisted,
)


def test_provider_credentials_are_dropped_and_the_rest_kept() -> None:
    env = _sanitize_subprocess_env(
        {
            "OPENAI_API_KEY": "sk-secret",
            "openai_api_key": "folded-on-windows",
            "GH_TOKEN": "ghp",
            "PATH": "/usr/bin",
            "HOME": "/home/me",
        }
    )

    assert env == {"PATH": "/usr/bin", "HOME": "/home/me"}


def test_internal_secrets_are_dropped() -> None:
    env = _sanitize_subprocess_env(
        {"AUXILIARY_VISION_API_KEY": "k", "GATEWAY_RELAY_X_TOKEN": "t", "GATEWAY_RELAY_URL": "u"}
    )

    assert env == {"GATEWAY_RELAY_URL": "u"}


def test_the_force_prefix_unwraps_only_in_caller_extras() -> None:
    env = _sanitize_subprocess_env(
        {"_MERTINA_FORCE_OPENAI_API_KEY": "from-parent"},
        {"_MERTINA_FORCE_OPENAI_API_KEY": "from-caller", "_MERTINA_FORCE_AUXILIARY_A_API_KEY": "x"},
    )

    assert env == {"OPENAI_API_KEY": "from-caller"}


def test_missing_parts_are_empty() -> None:
    assert _sanitize_subprocess_env(None) == {}


def test_the_policy_predicates() -> None:
    assert _is_provider_env_blocklisted("ANTHROPIC_API_KEY")
    assert _is_provider_env_blocklisted("anthropic_api_key")
    assert not _is_provider_env_blocklisted("CLAUDE_CODE_OAUTH_TOKEN")
    assert not _is_provider_env_blocklisted("PATH")
    assert _is_mertina_internal_secret("auxiliary_task_base_url")
    assert not _is_mertina_internal_secret("GATEWAY_RELAY_URL")
