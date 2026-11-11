"""Start a single-user loopback Gateway from validated environment settings."""

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from mertina_agent.config import Settings, load_settings
from mertina_agent.exceptions import MertinaError
from mertina_agent.gateway.run import GatewayRunner


async def serve(settings: Settings) -> None:
    """Listen until SIGINT, SIGTERM or task cancellation, then drain owned resources."""
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    handlers: list[signal.Signals] = []
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stopped.set)
                handlers.append(signum)
            except NotImplementedError:
                # Windows asyncio.run handles Ctrl+C by cancelling this coroutine.
                break
        async with GatewayRunner(settings) as runner:
            logging.getLogger(__name__).info("Gateway listening at %s", runner.address)
            await stopped.wait()
    finally:
        for signum in handlers:
            loop.remove_signal_handler(signum)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local Gateway and expose controlled startup or shutdown failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        settings = load_settings(env_file=args.env_file)
        logging.basicConfig(level=settings.log_level)
        for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2"):
            logging.getLogger(name).setLevel(logging.WARNING)
        asyncio.run(serve(settings))
    except (MertinaError, OSError, TimeoutError):
        sys.stderr.write(
            "Gateway failed to start or close; check configuration and database health.\n"
        )
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
