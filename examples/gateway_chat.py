"""Create/continue a session through HTTP, stream one run and read its messages.

Start the Gateway separately, then run ``python examples/gateway_chat.py 'Hello'``.
``--base-url`` selects a loopback Gateway; ``--session-id`` continues a saved
conversation. ``--stop-after SECONDS`` requests cooperative stop; ``--timeout``
bounds the whole client workflow. Progress is safe JSON on stderr, final state
and the first message page are JSON on stdout. Disconnecting this client leaves
the server's background run intact; only the stop endpoint requests interruption.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from contextlib import suppress

import aiohttp
from gateway_client import GatewayClient, add_common_arguments, identifier, nonnegative_seconds


def report(event: dict[str, object]) -> None:
    """Print progress without allowing model output to control the terminal."""
    sys.stderr.write(json.dumps(event, ensure_ascii=True) + "\n")


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Execute the create/start/stream/stop/history workflow using HTTP alone."""
    async with (
        asyncio.timeout(args.timeout),
        GatewayClient(args.base_url, timeout_s=args.timeout) as client,
    ):
        session_id = await client.ensure_session(args.session_id, title=args.title)
        started = await client.request(
            "POST", "/v1/runs", body={"session_id": session_id, "message": args.prompt}
        )
        run_id = identifier(started.get("run_id"))
        report({"type": "attached", "session_id": session_id, "run_id": run_id})

        async def stop_later() -> None:
            await asyncio.sleep(args.stop_after)
            await client.request("POST", "/v1/runs/" + run_id + "/stop")

        stop_task = asyncio.create_task(stop_later()) if args.stop_after is not None else None
        try:
            result = await client.follow(run_id, report=report)
        finally:
            if stop_task is not None:
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task
        messages = await client.request("GET", f"/api/sessions/{session_id}/messages?limit=10")
        return {"session_id": session_id, "run": result, "messages": messages}


def main(argv: Sequence[str] | None = None) -> int:
    """Run one HTTP conversation; return 0, failure 1, or keyboard interruption 130."""
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    parser.add_argument("--stop-after", type=nonnegative_seconds, help="request stop after seconds")
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(run(args))
    except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError):
        sys.stderr.write("Gateway request failed; check local server status and input.\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Client interrupted; the Gateway run may still be active.\n")
        return 130
    sys.stdout.write(json.dumps(summary, ensure_ascii=True, indent=2) + "\n")
    result = summary["run"]
    return 1 if isinstance(result, dict) and result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
