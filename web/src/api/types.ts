/**
 * Gateway resources as this UI sees them, with their runtime decoders.
 *
 * Field names follow the wire format, so a type can be checked against the Gateway's
 * README line by line. Fields added for the Web UI (Gateway CP1) are optional: an older
 * Gateway leaves them out and the UI degrades instead of failing.
 */

import {
  array,
  boolean,
  integer,
  isRecord,
  nullableInteger,
  nullableNumber,
  nullableString,
  oneOf,
  optional,
  ProtocolError,
  record,
  string,
  type JsonRecord,
} from "./decode";

export interface Session {
  readonly id: string;
  readonly title: string;
  readonly created_at_us: number;
  readonly updated_at_us: number;
  readonly history_revision: number;
  readonly metadata_version: number;
  readonly message_count: number;
  readonly active_run_id: string | null;
}

export type SessionCursor = Readonly<Record<string, string | number>>;

export interface SessionPage {
  readonly items: readonly Session[];
  readonly next_cursor: SessionCursor | null;
}

export interface ToolCallMessage {
  readonly id: string;
  readonly name: string;
  /** The arguments exactly as the model wrote them; not necessarily valid JSON. */
  readonly arguments: string;
}

export type ChatMessage =
  | { readonly role: "system" | "developer" | "user"; readonly content: string }
  | {
      readonly role: "assistant";
      readonly content: string | null;
      readonly tool_calls: readonly ToolCallMessage[];
      readonly refusal: string | null;
    }
  | { readonly role: "tool"; readonly content: string; readonly tool_call_id: string };

export interface StoredMessage {
  readonly seq: number;
  readonly message: ChatMessage;
}

export interface MessagePage {
  readonly items: readonly StoredMessage[];
  readonly through: number;
  readonly next_after: number | null;
}

export const RUN_STATUSES = [
  "pending",
  "running",
  "committing",
  "completed",
  "partial",
  "stopped",
  "failed",
] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

export const TERMINAL_STATUSES = ["completed", "partial", "stopped", "failed"] as const;
export type TerminalStatus = (typeof TERMINAL_STATUSES)[number];

/** The closed set of terminal `error` values; each one has its own UI treatment. */
export const TERMINAL_ERRORS = [
  "model_failed",
  "run_failed",
  "history_conflict",
  "storage_unconfirmed",
  "shutdown_cancelled",
] as const;
export type TerminalError = (typeof TERMINAL_ERRORS)[number];

export type ToolCallStatus = "running" | "ok" | "error";

export interface SnapshotToolCall {
  readonly call_id: string;
  readonly name: string;
  readonly status: ToolCallStatus;
  readonly duration_s: number | null;
  readonly is_error: boolean;
  readonly arguments: JsonRecord | null;
  readonly arguments_truncated: boolean;
}

export interface RunSnapshot {
  readonly run_id: string;
  readonly session_id: string;
  readonly status: RunStatus;
  readonly text: string;
  readonly text_truncated: boolean;
  /** A known terminal error, or any other string a newer Gateway may send. */
  readonly error: string | null;
  readonly durable: boolean;
  readonly terminal: boolean;
  readonly stop_requested: boolean;
  readonly reconciliation_required: boolean;
  readonly revision: number | null;
  readonly last_event_id: number;
  readonly accepted_at_us?: number;
  readonly finished_at_us?: number | null;
  readonly tool_calls?: readonly SnapshotToolCall[];
}

export type CommitReceipt =
  | {
      readonly operation_id: string;
      readonly committed: true;
      readonly revision: number;
      readonly first_seq: number;
      readonly last_seq: number;
      readonly status: string;
    }
  | { readonly operation_id: string; readonly committed: false };

export function decodeSession(value: unknown): Session {
  const where = "session";
  const source = record(value, where);
  return {
    id: string(source, "id", where),
    title: string(source, "title", where),
    created_at_us: integer(source, "created_at_us", where),
    updated_at_us: integer(source, "updated_at_us", where),
    history_revision: integer(source, "history_revision", where),
    metadata_version: integer(source, "metadata_version", where),
    message_count: integer(source, "message_count", where),
    active_run_id: nullableString(source, "active_run_id", where),
  };
}

function decodeCursor(value: unknown): SessionCursor | null {
  if (value === null) {
    return null;
  }
  const source = record(value, "next_cursor");
  const cursor: Record<string, string | number> = {};
  for (const [key, item] of Object.entries(source)) {
    if (typeof item !== "string" && typeof item !== "number") {
      throw new ProtocolError(`next_cursor.${key} must be a string or number`);
    }
    cursor[key] = item;
  }
  return cursor;
}

export function decodeSessionPage(value: unknown): SessionPage {
  const source = record(value, "session page");
  return {
    items: array(source.items, "session page.items").map(decodeSession),
    next_cursor: decodeCursor(source.next_cursor),
  };
}

function decodeToolCallMessage(value: unknown): ToolCallMessage {
  const where = "tool call";
  const source = record(value, where);
  const fn = record(source.function, `${where}.function`);
  return {
    id: string(source, "id", where),
    name: string(fn, "name", `${where}.function`),
    arguments: string(fn, "arguments", `${where}.function`),
  };
}

export function decodeChatMessage(value: unknown): ChatMessage {
  const where = "message";
  const source = record(value, where);
  const role = oneOf(source, "role", ["system", "developer", "user", "assistant", "tool"], where);
  switch (role) {
    case "assistant": {
      const calls = source.tool_calls;
      return {
        role,
        content: source.content === undefined ? null : nullableString(source, "content", where),
        tool_calls:
          calls === undefined || calls === null
            ? []
            : array(calls, `${where}.tool_calls`).map(decodeToolCallMessage),
        refusal: source.refusal === undefined ? null : nullableString(source, "refusal", where),
      };
    }
    case "tool":
      return {
        role,
        content: string(source, "content", where),
        tool_call_id: string(source, "tool_call_id", where),
      };
    default:
      return { role, content: string(source, "content", where) };
  }
}

export function decodeMessagePage(value: unknown): MessagePage {
  const where = "message page";
  const source = record(value, where);
  return {
    items: array(source.items, `${where}.items`).map((item) => {
      const entry = record(item, `${where}.item`);
      return {
        seq: integer(entry, "seq", `${where}.item`),
        message: decodeChatMessage(entry.message),
      };
    }),
    through: integer(source, "through", where),
    next_after: nullableInteger(source, "next_after", where),
  };
}

/** Arguments are any JSON object, or `null` when the Gateway withheld them for size. */
export function nullableJsonRecord(
  source: JsonRecord,
  key: string,
  where: string,
): JsonRecord | null {
  const value = source[key];
  if (value === null) {
    return null;
  }
  if (!isRecord(value)) {
    throw new ProtocolError(`${where}.${key} must be an object or null`);
  }
  return value;
}

function decodeSnapshotToolCall(value: unknown): SnapshotToolCall {
  const where = "snapshot tool call";
  const source = record(value, where);
  return {
    call_id: string(source, "call_id", where),
    name: string(source, "name", where),
    status: oneOf(source, "status", ["running", "ok", "error"], where),
    duration_s: nullableNumber(source, "duration_s", where),
    is_error: boolean(source, "is_error", where),
    arguments: nullableJsonRecord(source, "arguments", where),
    arguments_truncated: boolean(source, "arguments_truncated", where),
  };
}

export function decodeRunSnapshot(value: unknown): RunSnapshot {
  const where = "run";
  const source = record(value, where);
  const snapshot: RunSnapshot = {
    run_id: string(source, "run_id", where),
    session_id: string(source, "session_id", where),
    status: oneOf(source, "status", RUN_STATUSES, where),
    text: string(source, "text", where),
    text_truncated: boolean(source, "text_truncated", where),
    error: nullableString(source, "error", where),
    durable: boolean(source, "durable", where),
    terminal: boolean(source, "terminal", where),
    stop_requested: boolean(source, "stop_requested", where),
    reconciliation_required: boolean(source, "reconciliation_required", where),
    revision: nullableInteger(source, "revision", where),
    last_event_id: integer(source, "last_event_id", where),
  };
  const acceptedAt = optional(source, "accepted_at_us", integer, where);
  const finishedAt = optional(source, "finished_at_us", nullableInteger, where);
  const toolCalls =
    source.tool_calls === undefined
      ? undefined
      : array(source.tool_calls, `${where}.tool_calls`).map(decodeSnapshotToolCall);
  return {
    ...snapshot,
    ...(acceptedAt === undefined ? {} : { accepted_at_us: acceptedAt }),
    ...(finishedAt === undefined ? {} : { finished_at_us: finishedAt }),
    ...(toolCalls === undefined ? {} : { tool_calls: toolCalls }),
  };
}

export function decodeCommitReceipt(value: unknown): CommitReceipt {
  const where = "commit receipt";
  const source = record(value, where);
  const operationId = string(source, "operation_id", where);
  if (!boolean(source, "committed", where)) {
    return { operation_id: operationId, committed: false };
  }
  return {
    operation_id: operationId,
    committed: true,
    revision: integer(source, "revision", where),
    first_seq: integer(source, "first_seq", where),
    last_seq: integer(source, "last_seq", where),
    status: string(source, "status", where),
  };
}
