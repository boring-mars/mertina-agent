-- Mertina session tables, PostgreSQL.
-- Open with: docker compose -f dev/databases/compose.yaml exec postgres psql -U mertina -d mertina_dev
-- Replace SESSION_ID below with an id from the first query.

-- Sessions, newest first.
SELECT id, title, history_revision, metadata_version, message_count,
       to_timestamp(updated_at_us / 1000000.0) AS updated
FROM mertina_sessions ORDER BY updated_at_us DESC;

-- One session's messages, decoded. Each payload is {"version": 1, "message": {...}}.
SELECT seq,
       payload::jsonb -> 'message' ->> 'role' AS role,
       left(coalesce(payload::jsonb -> 'message' ->> 'content', payload::jsonb -> 'message' ->> 'refusal', ''), 80) AS text,
       payload::jsonb -> 'message' -> 'tool_calls' -> 0 -> 'function' ->> 'name' AS tool_call,
       payload::jsonb -> 'message' ->> 'tool_call_id' AS tool_call_id,
       octet_length(payload) AS bytes
FROM mertina_messages WHERE session_id = 'SESSION_ID' ORDER BY seq;

-- One session's commit receipts: one row per saved turn.
SELECT revision, first_seq, last_seq, status, operation_id
FROM mertina_commits WHERE session_id = 'SESSION_ID' ORDER BY revision;

-- Invariant: message_count matches the stored rows (expect no rows).
SELECT s.id, s.message_count, count(m.seq) AS stored_rows, max(m.seq) AS last_seq
FROM mertina_sessions s LEFT JOIN mertina_messages m ON m.session_id = s.id
GROUP BY s.id, s.message_count
HAVING s.message_count <> count(m.seq) OR s.message_count <> coalesce(max(m.seq), 0);

-- Space used per table, including indexes and TOAST.
SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) AS size
FROM pg_class WHERE relname LIKE 'mertina\_%' AND relkind = 'r';
