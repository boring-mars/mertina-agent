"""Configuration, read from the environment in one place.

Settings follow 12-factor: environment variables are the source of truth, every
variable carries the ``MERTINA_`` prefix, and a local ``.env`` file fills in the
gaps during development. :func:`load_settings` validates everything at startup so
an invalid value fails fast instead of surfacing halfway through a run.

See ``.env.example`` for the full list of variables with placeholder values.
"""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
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
        llm_timeout_s: Timeout for each network operation, not the whole turn.
        max_iterations: Model calls a single turn may spend before the agent
            gives up on the tool loop.
    """

    model_config = SettingsConfigDict(
        env_prefix="MERTINA_",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    # The model settings are named ``llm_*`` rather than ``model_*`` because
    # pydantic reserves the ``model_`` prefix for its own attributes.
    log_level: LogLevel = "INFO"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_s: float = Field(default=60.0, gt=0.0, allow_inf_nan=False)
    max_iterations: int = Field(default=10, ge=1)

    @field_validator("llm_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        """Reject unusable endpoints before constructing any network client."""
        message = "llm_base_url must be an HTTP(S) base URL without credentials, query or fragment"
        try:
            parsed = urlsplit(value)
            # Reading port also validates malformed/out-of-range port numbers.
            _ = parsed.port
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                # The SDK appends a resource path to raw_path; query/fragment
                # URLs would silently route to the wrong endpoint.
                and "?" not in value
                and "#" not in value
                and not any(
                    character.isspace() or ord(character) < 32 or ord(character) == 127
                    for character in value
                )
            )
        except ValueError:
            raise ValueError(message) from None
        if not valid:
            raise ValueError(message)
        return value

    @field_validator("llm_model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        """Require a name without rewriting the caller's model identifier."""
        if not value.strip():
            message = "llm_model must not be empty"
            raise ValueError(message)
        return value


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
        # A validation error may contain a URL credential or invalid secret.
        # Keep field locations, never echo the rejected input or SDK details.
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        message = f"Invalid Mertina configuration fields: {fields}"
        raise ConfigurationError(message) from exc
