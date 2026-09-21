"""Exception hierarchy for Mertina Agent.

Every error raised on purpose by this package derives from :class:`MertinaError`,
so callers can catch the whole package with a single ``except``.
"""

from typing import Literal


class MertinaError(Exception):
    """Base class for every error raised by Mertina Agent."""


class ConfigurationError(MertinaError):
    """Raised when configuration is missing, malformed or out of range."""


class ToolRegistrationError(MertinaError):
    """Raised when a tool declaration is invalid or would shadow another toolset."""


class ModelInputError(MertinaError):
    """Raised before dispatch when messages or tools violate the text contract."""


class ModelResponseError(MertinaError):
    """Raised when a provider response violates the non-streaming contract."""


type RequestErrorKind = Literal["timeout", "connection", "http", "closed"]


class ModelRequestError(MertinaError):
    """Expose safe request failure metadata without provider bodies or prompts.

    The original SDK exception remains available as ``__cause__`` for trusted
    debugging. User-facing handlers must display this error, not its traceback:
    the cause can contain credentials, request content or a provider error body.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: RequestErrorKind,
        status_code: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        """Store the failure category and optional HTTP retry metadata."""
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after
