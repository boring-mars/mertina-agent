"""Exception hierarchy for Mertina Agent.

Every error raised on purpose by this package derives from :class:`MertinaError`,
so callers can catch the whole package with a single ``except``.
"""


class MertinaError(Exception):
    """Base class for every error raised by Mertina Agent."""


class ConfigurationError(MertinaError):
    """Raised when configuration is missing, malformed or out of range."""
