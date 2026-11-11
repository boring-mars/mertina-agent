"""Disconnect from SSE, then resume the same run through replay or a fresh snapshot.

Use ``--disconnect-after N`` to close after N events (default 1), and
``--reconnect-delay SECONDS`` to wait before attaching again. ``--session-id``
continues a saved session, ``--base-url`` chooses a loopback Gateway, and
``--timeout`` bounds the full demonstration. The final JSON includes the same
run ID, final state and its persisted messages. No new run is created on reconnect.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from contextlib import aclosing

import aiohttp
from gateway_client import (
    GatewayClient,
    add_common_arguments,
    identifier,
    nonnegative_seconds,
    number,
)


def _event_count(value: str) -> int:
    try:
        count = int(value)
        if not 1 <= count <= 10000:
            raise ValueError
    except ValueError as exc:
        message = "Event count must be between 1 and 10000"
        raise argparse.ArgumentTypeError(message) from exc
    return count


def report(event: dict[str, object]) -> None:
    """Escape all progress as JSON, including the snapshot reset notification."""
    sys.stderr.write(json.dumps(event, ensure_ascii=True) + "\n")


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Close one subscriber, retain its cursor and attach to that exact run again."""
    async with (
        asyncio.timeout(args.timeout),
        GatewayClient(args.base_url, timeout_s=args.timeout) as client,
    ):
        session_id = await client.ensure_session(args.session_id, title=args.title)
        started = await client.request(
            "POST", "/v1/runs", body={"session_id": session_id, "message": args.prompt}
        )
        run_id = identifier(started.get("run_id"))
        cursor = 0
        seen = 0
        async with aclosing(client.events(run_id)) as events:
            async for event in events:
                if event.get("type") == "resync_required":
                    break
                cursor = number(event.get("id"))
                report(event)
                seen += 1
                if seen >= args.disconnect_after:
                    break
        report({"type": "disconnected", "run_id": run_id, "last_event_id": cursor})
        await asyncio.sleep(args.reconnect_delay)
        result = await client.follow(run_id, after=cursor, report=report)
        if result.get("run_id") != run_id:
            message = "Reconnection changed the run identity"
            raise RuntimeError(message)
        messages = await client.request("GET", f"/api/sessions/{session_id}/messages?limit=10")
        return {
            "session_id": session_id,
            "run_id": run_id,
            "disconnected_after_event_id": cursor,
            "run": result,
            "messages": messages,
        }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the HTTP reconnection demonstration with safe process exit statuses."""
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    parser.add_argument("--disconnect-after", type=_event_count, default=1)
    parser.add_argument("--reconnect-delay", type=nonnegative_seconds, default=0)
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(run(args))
    except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError):
        sys.stderr.write("Gateway reconnection failed; check local server status and input.\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Client interrupted; the Gateway run may still be active.\n")
        return 130
    sys.stdout.write(json.dumps(summary, ensure_ascii=True, indent=2) + "\n")
    result = summary["run"]
    return 1 if isinstance(result, dict) and result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
