"""Inspect the session tables of any supported backend and check their invariants.

The same code reads SQLite, PostgreSQL and MySQL through the project's own
database adapter and schema, so results are comparable across backends.

Checks for every session:

- ``seq`` runs 1..``message_count`` without gaps, and every payload decodes.
- Commit receipts, ordered by revision, cover consecutive ``seq`` ranges ending
  at ``message_count``, and the last revision equals ``history_revision``.
- No messages or receipts exist for sessions that were deleted.

Command line, for looking at a database by hand::

    uv run python dev/e2e/inspect_db.py --env-file .env
    uv run python dev/e2e/inspect_db.py --env-file .env --session SESSION_ID
    uv run python dev/e2e/inspect_db.py --env-file .env --json
"""

import argparse
import asyncio
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import bindparam, select, text

from mertina_agent.config import Settings, load_settings
from mertina_agent.database.runtime import Database, open_database
from mertina_agent.state.schema import Schema
from mertina_agent.state.serialization import decode_message

type Report = dict[str, object]


async def storage_bytes(database: Database, schema: Schema) -> int:
    """Space used by the four session tables, as the backend reports it."""
    names = [table.name for table in schema.metadata.sorted_tables]
    if database.backend == "sqlite":
        async with database.transaction(write=False) as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()"
                    )
                )
            ).one()
        return int(row[0])
    if database.backend == "postgresql":
        total = 0
        async with database.transaction(write=False) as connection:
            for name in names:
                query = text("SELECT pg_total_relation_size(CAST(:name AS regclass))")
                total += int((await connection.execute(query, {"name": name})).scalar_one())
        return total
    async with database.transaction() as connection:
        # MySQL caches table statistics; refresh them before reading sizes.
        await connection.execute(text("SET SESSION information_schema_stats_expiry = 0"))
        for name in names:
            await connection.execute(text(f"ANALYZE TABLE `{name}`"))
        query = text(
            "SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name IN :names"
        ).bindparams(bindparam("names", expanding=True))
        return int((await connection.execute(query, {"names": names})).scalar_one())


def _preview(value: object, limit: int = 70) -> str:
    line = " ".join(str(value).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


async def inspect(settings: Settings, *, deleted_ids: Iterable[str] = ()) -> Report:
    """Read every session, message and receipt, then check the invariants.

    Args:
        settings: Settings naming the database to read.
        deleted_ids: Sessions deleted during the test; none of their rows may remain.

    Returns:
        Per-session summaries (with a SHA-256 per stored payload), the problems
        found, the backend name and the storage size.
    """
    schema = Schema()
    async with await open_database(settings) as database:
        await schema.initialize(database, bootstrap=False)
        async with database.transaction(write=False) as connection:
            sessions = (
                (
                    await connection.execute(
                        select(schema.sessions).order_by(schema.sessions.c.created_at_us)
                    )
                )
                .mappings()
                .all()
            )
            message_rows = (
                (
                    await connection.execute(
                        select(schema.messages).order_by(
                            schema.messages.c.session_id, schema.messages.c.seq
                        )
                    )
                )
                .mappings()
                .all()
            )
            commit_rows = (
                (
                    await connection.execute(
                        select(schema.commits).order_by(
                            schema.commits.c.session_id, schema.commits.c.revision
                        )
                    )
                )
                .mappings()
                .all()
            )
        size = await storage_bytes(database, schema)
        backend = database.backend

    messages: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in message_rows:
        messages[row["session_id"]].append(dict(row))
    commits: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in commit_rows:
        commits[row["session_id"]].append(dict(row))

    problems: list[str] = []
    summaries: list[Report] = []
    known = {row["id"] for row in sessions}
    for row in sessions:
        sid = row["id"]
        stored = messages.get(sid, [])
        seqs = [int(item["seq"]) for item in stored]
        if seqs != list(range(1, row["message_count"] + 1)):
            problems.append(f"{sid}: seq is not 1..{row['message_count']}")
        roles: list[str] = []
        for item in stored:
            try:
                roles.append(str(decode_message(str(item["payload"]))["role"]))
            except Exception as exc:  # report it and keep scanning
                problems.append(f"{sid}: seq {item['seq']} does not decode ({type(exc).__name__})")
        receipts = commits.get(sid, [])
        expected_first = 1
        for receipt in receipts:
            if receipt["first_seq"] != expected_first:
                problems.append(f"{sid}: revision {receipt['revision']} starts at a gap")
            expected_first = int(receipt["last_seq"]) + 1
        if receipts and receipts[-1]["last_seq"] != row["message_count"]:
            problems.append(f"{sid}: receipts end before message_count")
        if [receipt["revision"] for receipt in receipts] != list(
            range(1, row["history_revision"] + 1)
        ):
            problems.append(f"{sid}: receipt revisions do not match history_revision")
        summaries.append(
            {
                "id": sid,
                "title": row["title"],
                "message_count": row["message_count"],
                "history_revision": row["history_revision"],
                "metadata_version": row["metadata_version"],
                "roles": roles,
                "statuses": [receipt["status"] for receipt in receipts],
                "payload_bytes": sum(len(str(item["payload"]).encode("utf-8")) for item in stored),
                "hashes": [
                    hashlib.sha256(str(item["payload"]).encode("utf-8")).hexdigest()
                    for item in stored
                ],
            }
        )
    for sid in set(messages) | set(commits):
        if sid not in known:
            problems.append(f"{sid}: rows remain for a missing session")
    for sid in deleted_ids:
        if sid in known or sid in messages or sid in commits:
            problems.append(f"{sid}: deleted session still has rows")
    return {
        "backend": backend,
        "storage_bytes": size,
        "sessions": summaries,
        "problems": problems,
        "messages": len(message_rows),
        "commits": len(commit_rows),
    }


async def _transcript(settings: Settings, session_id: str) -> list[str]:
    schema = Schema()
    lines: list[str] = []
    async with await open_database(settings) as database:
        await schema.initialize(database, bootstrap=False)
        async with database.transaction(write=False) as connection:
            rows = (
                await connection.execute(
                    select(schema.messages)
                    .where(schema.messages.c.session_id == session_id)
                    .order_by(schema.messages.c.seq)
                )
            ).mappings()
            for row in rows:
                message = decode_message(str(row["payload"]))
                calls = message.get("tool_calls") or []
                detail = " ".join(f"→ {call['function']['name']}({call['id']})" for call in calls)
                reference = message.get("tool_call_id")
                content = message.get("content") or message.get("refusal") or ""
                tag = f"[{reference}] " if reference else ""
                lines.append(
                    f"  {row['seq']:>4}  {message['role']:<9} "
                    f"{len(str(row['payload'])):>7} B  {tag}{_preview(content)} {detail}"
                )
            receipts = (
                await connection.execute(
                    select(schema.commits)
                    .where(schema.commits.c.session_id == session_id)
                    .order_by(schema.commits.c.revision)
                )
            ).mappings()
            lines.append("  commits:")
            lines.extend(
                f"    rev {row['revision']}: seq {row['first_seq']}-{row['last_seq']} "
                f"{row['status']} op={row['operation_id']}"
                for row in receipts
            )
    return lines


def main() -> int:
    """Print sessions and invariant checks, or one session's transcript."""
    parser = argparse.ArgumentParser(description="Inspect Mertina session tables.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--session", help="print this session's messages and receipts")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()
    settings = load_settings(env_file=args.env_file)
    out = sys.stdout
    if args.session:
        out.write("\n".join(asyncio.run(_transcript(settings, args.session))) + "\n")
        return 0
    report = asyncio.run(inspect(settings))
    if args.json:
        out.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return 0
    out.write(
        f"{report['backend']}: {len(report['sessions'])} sessions, {report['messages']} messages, "
        f"{report['commits']} commits, {report['storage_bytes']:,} bytes\n"
    )
    for session in report["sessions"]:
        out.write(
            f"  {session['id']}  rev {session['history_revision']:>3}  "
            f"{session['message_count']:>4} msgs  {' '.join(sorted(set(session['statuses'])))}  "
            f"{_preview(session['title'], 50)}\n"
        )
    problems = report["problems"]
    out.write("Invariants: all hold\n" if not problems else "Problems:\n")
    for problem in problems:
        out.write(f"  - {problem}\n")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
