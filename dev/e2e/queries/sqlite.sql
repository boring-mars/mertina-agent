-- Mertina session tables, SQLite.
-- Open with: uv run python -m sqlite3 /path/to/mertina.sqlite3
-- Replace SESSION_ID below with an id from the first query.

-- Sessions, newest first.
SELECT id, title, history_revision, metadata_version, message_count,
       datetime(updated_at_us / 1000000, 'unixepoch', 'localtime') AS updated
FROM mertina_sessions ORDER BY updated_at_us DESC;

-- One session's messages, decoded. Each payload is {"version": 1, "message": {...}}.
SELECT seq,
       json_extract(payload, '$.message.role') AS role,
       substr(coalesce(json_extract(payload, '$.message.content'), json_extract(payload, '$.message.refusal'), ''), 1, 80) AS text,
       json_extract(payload, '$.message.tool_calls[0].function.name') AS tool_call,
       json_extract(payload, '$.message.tool_call_id') AS tool_call_id,
       length(CAST(payload AS BLOB)) AS bytes
FROM mertina_messages WHERE session_id = 'SESSION_ID' ORDER BY seq;

-- One session's commit receipts: one row per saved turn.
SELECT revision, first_seq, last_seq, status, operation_id
FROM mertina_commits WHERE session_id = 'SESSION_ID' ORDER BY revision;

-- Invariant: message_count matches the stored rows (expect no rows).
SELECT s.id, s.message_count, count(m.seq) AS stored_rows, max(m.seq) AS last_seq
FROM mertina_sessions s LEFT JOIN mertina_messages m ON m.session_id = s.id
GROUP BY s.id HAVING s.message_count <> count(m.seq) OR s.message_count <> coalesce(max(m.seq), 0);

-- Schema version.
SELECT * FROM mertina_schema_version;
