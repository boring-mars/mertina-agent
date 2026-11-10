-- Mertina session tables, MySQL.
-- Open with: docker compose -f dev/databases/compose.yaml exec mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u "$MYSQL_USER" "$MYSQL_DATABASE"'
-- Replace SESSION_ID below with an id from the first query.

-- Sessions, newest first.
SELECT id, title, history_revision, metadata_version, message_count,
       FROM_UNIXTIME(updated_at_us / 1000000) AS updated
FROM mertina_sessions ORDER BY updated_at_us DESC;

-- One session's messages, decoded. Each payload is {"version": 1, "message": {...}}.
SELECT seq,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.message.role')) AS role,
       LEFT(COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(payload, '$.message.content')), 'null'),
                     NULLIF(JSON_UNQUOTE(JSON_EXTRACT(payload, '$.message.refusal')), 'null'),
                     ''), 80) AS text,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.message.tool_calls[0].function.name')) AS tool_call,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.message.tool_call_id')) AS tool_call_id,
       LENGTH(payload) AS bytes
FROM mertina_messages WHERE session_id = 'SESSION_ID' ORDER BY seq;

-- One session's commit receipts: one row per saved turn.
SELECT revision, first_seq, last_seq, status, operation_id
FROM mertina_commits WHERE session_id = 'SESSION_ID' ORDER BY revision;

-- Invariant: message_count matches the stored rows (expect no rows).
SELECT s.id, s.message_count, COUNT(m.seq) AS stored_rows, MAX(m.seq) AS last_seq
FROM mertina_sessions s LEFT JOIN mertina_messages m ON m.session_id = s.id
GROUP BY s.id, s.message_count
HAVING s.message_count <> COUNT(m.seq) OR s.message_count <> COALESCE(MAX(m.seq), 0);

-- Table engine, collation and size (run ANALYZE TABLE first for fresh sizes).
SELECT table_name, engine, table_collation, data_length + index_length AS bytes
FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name LIKE 'mertina\_%';
