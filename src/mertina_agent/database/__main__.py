"""Check the configured database without creating application tables."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from mertina_agent.config import load_settings
from mertina_agent.database.runtime import open_database
from mertina_agent.exceptions import ConfigurationError, DatabaseError


async def _check(env_file: Path) -> None:
    async with await open_database(load_settings(env_file=env_file)) as database:
        sys.stdout.write(f"{database.backend}: connection healthy\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run a bounded health check with controlled error output and no DSN."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        asyncio.run(_check(args.env_file))
    except (ConfigurationError, DatabaseError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
