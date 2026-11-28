/**
 * Follow one run's event stream until it ends: subscribe, reconnect, resynchronize.
 *
 * The controller holds no display state. It hands decoded events and snapshots to the
 * caller, which feeds them to the reducer. Time and sleeping are injected so tests run
 * without real timers.
 */

import type { GatewayClient } from "@/api/client";
import { GatewayError, GatewayUnreachableError } from "@/api/errors";
import { readRunEvents } from "@/api/sse";
import type { RunSnapshot } from "@/api/types";

import { decodeRunEvent, isTerminalType, type RunEvent } from "./events";

export type ConnectionState = "connecting" | "live" | "reconnecting";

/** Why following stopped. */
export type FollowResult =
  /** A terminal event or a terminal snapshot was delivered. */
  | "terminal"
  /** The run is unknown: it expired or the Gateway restarted. Reload the session. */
  | "expired"
  /** The caller aborted. */
  | "aborted";

export interface FollowOptions {
  readonly client: GatewayClient;
  readonly runId: string;
  /** Last event ID already applied; 0 replays every buffered event. */
  readonly after: number;
  readonly onEvent: (event: RunEvent) => void;
  /** A snapshot replaced the event history; apply it with `applySnapshot`. */
  readonly onSnapshot: (snapshot: RunSnapshot) => void;
  readonly onConnection?: (state: ConnectionState) => void;
  readonly signal?: AbortSignal;
  /** Resolve after `ms`, or reject when `signal` aborts. */
  readonly sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  readonly initialBackoffMs?: number;
  readonly maxBackoffMs?: number;
}

export const INITIAL_BACKOFF_MS = 500;
export const MAX_BACKOFF_MS = 10_000;

export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted === true) {
      reject(signal.reason as Error);
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, ms);
    const abort = () => {
      clearTimeout(timer);
      reject(signal?.reason as Error);
    };
    signal?.addEventListener("abort", abort, { once: true });
  });
}

/** Delay before reconnect attempt `attempt` (0-based): doubling, capped. */
export function backoffMs(attempt: number, initial: number, max: number): number {
  return Math.min(max, initial * 2 ** attempt);
}

class Resync extends Error {}

/** Read `aborted` through a call, so narrowing does not survive across awaits. */
function isAborted(signal: AbortSignal | undefined): boolean {
  return signal?.aborted === true;
}

/**
 * Follow a run until it ends. Errors the client cannot recover from by retrying (a
 * rejected request, a malformed event) are thrown; connection failures and Gateway
 * unavailability are retried with backoff for as long as the caller lets it run.
 */
export async function followRun(options: FollowOptions): Promise<FollowResult> {
  const { client, runId, signal } = options;
  const wait = options.sleep ?? sleep;
  const initial = options.initialBackoffMs ?? INITIAL_BACKOFF_MS;
  const max = options.maxBackoffMs ?? MAX_BACKOFF_MS;
  let cursor = options.after;
  let failures = 0;
  options.onConnection?.("connecting");

  const resync = async (): Promise<FollowResult | null> => {
    const snapshot = await client.getRun(runId, signal);
    options.onSnapshot(snapshot);
    cursor = snapshot.last_event_id;
    return snapshot.terminal ? "terminal" : null;
  };

  for (;;) {
    if (isAborted(signal)) {
      return "aborted";
    }
    try {
      try {
        for await (const item of readRunEvents(client, runId, cursor, signal)) {
          if (item.kind === "resync") {
            throw new Resync();
          }
          failures = 0;
          options.onConnection?.("live");
          const event = decodeRunEvent(item.envelope);
          if (event.id <= cursor) {
            continue;
          }
          cursor = event.id;
          options.onEvent(event);
          if (isTerminalType(item.envelope.type)) {
            return "terminal";
          }
        }
      } catch (error) {
        const stale = error instanceof GatewayError && error.code === "resync_required";
        if (!(error instanceof Resync) && !stale) {
          throw error;
        }
        const result = await resync();
        if (result !== null) {
          return result;
        }
        continue;
      }
      // The stream ended without a terminal event: the Gateway closed it (for example
      // while shutting down). Reconnect from the cursor like after a network failure.
    } catch (error) {
      if (isAborted(signal)) {
        return "aborted";
      }
      if (error instanceof GatewayError && error.status === 404) {
        return "expired";
      }
      if (!isRetryable(error)) {
        throw error;
      }
    }
    options.onConnection?.("reconnecting");
    try {
      await wait(backoffMs(failures, initial, max), signal);
    } catch {
      return "aborted";
    }
    failures += 1;
  }
}

function isRetryable(error: unknown): boolean {
  if (error instanceof GatewayError) {
    return error.status >= 500 || error.status === 408;
  }
  // A failed connection, or a stream cut in the middle of a read (a TypeError in fetch).
  return error instanceof GatewayUnreachableError || error instanceof TypeError;
}
