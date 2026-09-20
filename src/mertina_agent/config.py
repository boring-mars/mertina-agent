"""Configuration, read from the environment in one place.

Settings follow 12-factor: environment variables are the source of truth, every
variable carries the ``MERTINA_`` prefix, and a local ``.env`` file fills in the
gaps during development. :func:`load_settings` validates everything at startup so
an invalid value fails fast instead of surfacing halfway through a run.

See ``.env.example`` for the full list of variables with placeholder values.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from mertina_agent.exceptions import ConfigurationError

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
"""Levels accepted by :mod:`logging`, spelled as they are in the environment."""

DEFAULT_ENV_FILE = ".env"
"""Env file read by :func:`load_settings` unless the caller says otherwise."""


class Settings(BaseSettings):
    """Runtime configuration of an agent process.

    Instances are immutable: configuration is read once at startup and passed
    around explicitly, rather than re-read or mutated later.

    Attributes:
        log_level: Threshold for the package's loggers.
        llm_base_url: Base URL of an OpenAI-compatible Chat Completions endpoint.
        llm_api_key: Credential for that endpoint. ``None`` when it needs none,
            as a local model server usually does.
        llm_model: Model name sent with every request.
        llm_timeout_s: Timeout applied to a single model request.
        max_iterations: Model calls a single turn may spend before the agent
            gives up on the tool loop.
    """

    model_config = SettingsConfigDict(
        env_prefix="MERTINA_",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    log_level: LogLevel = "INFO"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_s: float = Field(default=60.0, gt=0.0)
    max_iterations: int = Field(default=10, ge=1)


def load_settings(*, env_file: Path | str | None = DEFAULT_ENV_FILE) -> Settings:
    """Read and validate configuration from the environment.

    Args:
        env_file: Env file to read before the process environment, which always
            wins over the file. ``None`` reads the process environment only,
            which is what tests and production deployments want.

    Returns:
        The validated settings.

    Raises:
        ConfigurationError: If any variable is malformed or out of range.
    """
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        message = f"Invalid Mertina configuration:\n{exc}"
        raise ConfigurationError(message) from exc
