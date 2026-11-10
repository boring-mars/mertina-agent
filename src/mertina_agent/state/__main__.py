"""Explicitly initialize or validate the configured session storage schema."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from mertina_agent.config import load_settings
from mertina_agent.database.runtime import open_database
from mertina_agent.exceptions import ConfigurationError, DatabaseError, StorageError
from mertina_agent.state.schema import Schema


async def _execute(command: str, env_file: Path) -> None:
    async with await open_database(load_settings(env_file=env_file)) as database:
        await Schema().initialize(database, bootstrap=command == "init")
        sys.stdout.write(f"{database.backend}: session schema v1 ready\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Initialize only empty namespaces, or check v1, without logging credentials.

    ``init`` is idempotent on a valid v1 schema. Both commands reject partial or
    unknown versions; an initialization command never performs a migration.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "check"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        asyncio.run(_execute(args.command, args.env_file))
    except (ConfigurationError, DatabaseError, StorageError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
