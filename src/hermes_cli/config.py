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




def read_raw_config_readonly() -> Dict[str, Any]:
    return load_config_readonly()




def get_env_value(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return default if value is None else value


def ensure_hermes_home() -> Path:
    path = get_config_path().resolve().parent
    path.mkdir(parents=True, exist_ok=True)
    return path










def get_custom_provider_session_affinity_header(*_args, **_kwargs):
    return None
