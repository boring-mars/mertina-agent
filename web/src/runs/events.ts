/**
 * Typed run events, decoded from Gateway event envelopes.
 *
 * Event types this UI does not know are kept as `unknown` events instead of failing, so
 * an older UI keeps working when the Gateway adds an event.
 */

import {
  boolean,
  integer,
  nullableInteger,
  nullableString,
  number,
  oneOf,
  optional,
  string,
  type JsonRecord,
} from "@/api/decode";
import type { EventEnvelope } from "@/api/sse";
import { nullableJsonRecord, TERMINAL_STATUSES, type TerminalStatus } from "@/api/types";

interface Base {
  readonly id: number;
  readonly runId: string;
}

export type RunEvent = Base &
  (
    | { readonly type: "run.accepted" }
    | { readonly type: "run.started" }
    | { readonly type: "run.stop_requested" }
    | { readonly type: "run.committing" }
    | { readonly type: "text.delta"; readonly text: string }
    | { readonly type: "tool.generating"; readonly name: string }
    | {
        readonly type: "tool.started";
        readonly callId: string;
        readonly name: string;
        /** `undefined` when the Gateway predates tool arguments in events. */
        readonly arguments?: JsonRecord | null;
        readonly argumentsTruncated?: boolean;
      }
    | {
        readonly type: "tool.finished";
        readonly callId: string;
        readonly name: string;
        readonly durationS: number;
        readonly isError: boolean;
      }
    | {
        readonly type: "retry.scheduled";
        readonly attempt: number;
        readonly maxAttempts: number;
        readonly waitS: number;
        readonly reason: string;
      }
    | {
        readonly type: "run.terminal";
        readonly status: TerminalStatus;
        readonly durable: boolean;
        readonly revision: number | null;
        readonly error: string | null;
        readonly reconciliationRequired: boolean;
        readonly textTruncated?: boolean;
        readonly finishedAtUs?: number;
      }
    | { readonly type: "unknown"; readonly name: string; readonly data: JsonRecord }
  );

const TERMINAL_TYPES: ReadonlyMap<string, TerminalStatus> = new Map(
  TERMINAL_STATUSES.map((status) => [`run.${status}`, status]),
);

export function isTerminalType(type: string): boolean {
  return TERMINAL_TYPES.has(type);
}

/** Decode an envelope; a malformed payload of a known type is a `ProtocolError`. */
export function decodeRunEvent(envelope: EventEnvelope): RunEvent {
  const base = { id: envelope.id, runId: envelope.run_id };
  const data = envelope.data;
  const where = envelope.type;
  switch (envelope.type) {
    case "run.accepted":
    case "run.started":
    case "run.stop_requested":
    case "run.committing":
      return { ...base, type: envelope.type };
    case "text.delta":
      return { ...base, type: "text.delta", text: string(data, "text", where) };
    case "tool.generating":
      return { ...base, type: "tool.generating", name: string(data, "name", where) };
    case "tool.started": {
      const args =
        data.arguments === undefined ? undefined : nullableJsonRecord(data, "arguments", where);
      const truncated = optional(data, "arguments_truncated", boolean, where);
      return {
        ...base,
        type: "tool.started",
        callId: string(data, "call_id", where),
        name: string(data, "name", where),
        ...(args === undefined ? {} : { arguments: args }),
        ...(truncated === undefined ? {} : { argumentsTruncated: truncated }),
      };
    }
    case "tool.finished":
      return {
        ...base,
        type: "tool.finished",
        callId: string(data, "call_id", where),
        name: string(data, "name", where),
        durationS: number(data, "duration_s", where),
        isError: boolean(data, "is_error", where),
      };
    case "retry.scheduled":
      return {
        ...base,
        type: "retry.scheduled",
        attempt: integer(data, "attempt", where),
        maxAttempts: integer(data, "max_attempts", where),
        waitS: number(data, "wait_s", where),
        reason: string(data, "reason", where),
      };
    default:
      break;
  }
  const status = TERMINAL_TYPES.get(envelope.type);
  if (status === undefined) {
    return { ...base, type: "unknown", name: envelope.type, data };
  }
  const textTruncated = optional(data, "text_truncated", boolean, where);
  const finishedAtUs = optional(data, "finished_at_us", integer, where);
  return {
    ...base,
    type: "run.terminal",
    status: oneOf(data, "status", [status], where),
    durable: boolean(data, "durable", where),
    revision: nullableInteger(data, "revision", where),
    error: nullableString(data, "error", where),
    reconciliationRequired: boolean(data, "reconciliation_required", where),
    ...(textTruncated === undefined ? {} : { textTruncated }),
    ...(finishedAtUs === undefined ? {} : { finishedAtUs }),
  };
}
