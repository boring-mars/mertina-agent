"""Mertina v0.1 configuration: ``mertina.toml`` plus environment variables."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional


def get_config_path() -> Path:
    return Path(os.environ.get("MERTINA_CONFIG", "mertina.toml"))


def load_config_readonly() -> Dict[str, Any]:
    try:
        with get_config_path().open("rb") as config_file:
            return tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load_config() -> Dict[str, Any]:
    return load_config_readonly()


def read_raw_config() -> Dict[str, Any]:
    return load_config_readonly()


def read_raw_config_readonly() -> Dict[str, Any]:
    return load_config_readonly()


def cfg_get(cfg: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    """Safely walk nested config dictionaries; return ``default`` on any miss."""
    current: Any = cfg
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def get_env_value(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return default if value is None else value


def ensure_hermes_home() -> Path:
    path = get_config_path().resolve().parent
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_compatible_custom_providers(*_args, **_kwargs) -> Dict[str, Any]:
    return {}


def get_custom_provider_context_length(*_args, **_kwargs):
    return None


def get_custom_provider_tls_settings(*_args, **_kwargs) -> Dict[str, Any]:
    return {}


def get_custom_provider_api_mode(*_args, **_kwargs):
    return None


def get_custom_provider_session_affinity_header(*_args, **_kwargs):
    return None


def apply_custom_provider_extra_headers_to_client_kwargs(*_args, **_kwargs) -> None:
    return None


def split_model_config_default(value):
    return (None, value) if isinstance(value, str) else (None, None)
