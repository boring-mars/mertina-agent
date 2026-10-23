"""Validate database configuration without importing any driver."""

from urllib.parse import urlsplit

DRIVERS = {
    "sqlite": "sqlite+aiosqlite",
    "sqlite+aiosqlite": "sqlite+aiosqlite",
    "postgresql": "postgresql+asyncpg",
    "postgresql+asyncpg": "postgresql+asyncpg",
    "mysql": "mysql+asyncmy",
    "mysql+asyncmy": "mysql+asyncmy",
}


def validate_database_url(value: str) -> str:
    """Require a supported URL; connection options are separate typed settings."""
    message = "database_url must select SQLite, PostgreSQL or MySQL with a valid target"
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (
            parsed.scheme in DRIVERS
            and not parsed.query
            and not parsed.fragment
            and "?" not in value
            and "#" not in value
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )
        if parsed.scheme.startswith("sqlite"):
            valid = valid and not parsed.netloc and bool(parsed.path.strip("/"))
        else:
            valid = (
                valid
                and bool(parsed.hostname)
                and bool(parsed.username)
                and bool(parsed.path.strip("/"))
                and (port is None or port > 0)
                and not any(char.isspace() for char in value)
            )
    except ValueError:
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)
    return value
