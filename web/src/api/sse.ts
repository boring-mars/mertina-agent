/**
 * Read a run's Server-Sent Events with `fetch`.
 *
 * `EventSource` cannot send `Last-Event-ID` on the first request or expose the JSON 409
 * that tells the client to resynchronize, so the stream is read from a `ReadableStream`
 * and parsed here.
 */

import type { GatewayClient } from "./client";
import { integer, ProtocolError, record, string, type JsonRecord } from "./decode";

/** One parsed SSE frame. Comment lines (such as keepalives) produce no frame. */
export interface SseFrame {
  readonly id: string | null;
  readonly event: string;
  readonly data: string;
}

/**
 * Incremental SSE parser, following the WHATWG event-stream rules for the fields the
 * Gateway sends: `id`, `event` and `data`. Chunks may split lines, and even CRLF pairs,
 * anywhere.
 */
export class SseParser {
  #buffer = "";
  #id: string | null = null;
  #event = "";
  #data: string[] = [];
  #pendingCarriageReturn = false;

  /** Feed decoded text and return the frames it completed. */
  push(chunk: string): SseFrame[] {
    if (chunk === "") {
      return [];
    }
    let text = chunk;
    if (this.#pendingCarriageReturn && text.startsWith("\n")) {
      text = text.slice(1);
    }
    this.#pendingCarriageReturn = text.endsWith("\r");
    this.#buffer += text;
    const frames: SseFrame[] = [];
    const lines = this.#buffer.split(/\r\n|\r|\n/);
    this.#buffer = lines.pop() ?? "";
    for (const line of lines) {
      const frame = this.#line(line);
      if (frame !== null) {
        frames.push(frame);
      }
    }
    return frames;
  }

  #line(line: string): SseFrame | null {
    if (line === "") {
      if (this.#data.length === 0) {
        this.#event = "";
        return null;
      }
      const frame = {
        id: this.#id,
        event: this.#event || "message",
        data: this.#data.join("\n"),
      };
      this.#event = "";
      this.#data = [];
      return frame;
    }
    if (line.startsWith(":")) {
      return null;
    }
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) {
      value = value.slice(1);
    }
    if (field === "data") {
      this.#data.push(value);
    } else if (field === "event") {
      this.#event = value;
    } else if (field === "id" && !value.includes("\0")) {
      this.#id = value;
    }
    return null;
  }
}

/** The envelope the Gateway sends as each event's `data`. */
export interface EventEnvelope {
  readonly id: number;
  readonly run_id: string;
  readonly type: string;
  readonly data: JsonRecord;
}

export type StreamItem =
  | { readonly kind: "event"; readonly envelope: EventEnvelope }
  /** The Gateway dropped this subscriber; the client must read a snapshot. */
  | { readonly kind: "resync" };

export function decodeEnvelope(frame: SseFrame): EventEnvelope {
  let parsed: unknown;
  try {
    parsed = JSON.parse(frame.data);
  } catch (error) {
    throw new ProtocolError("Event data is not JSON", { cause: error });
  }
  const where = "event";
  const source = record(parsed, where);
  const envelope = {
    id: integer(source, "id", where),
    run_id: string(source, "run_id", where),
    type: string(source, "type", where),
    data: record(source.data, `${where}.data`),
  };
  if (frame.id !== String(envelope.id) || frame.event !== envelope.type) {
    throw new ProtocolError("Event frame does not match its envelope");
  }
  return envelope;
}

/**
 * Subscribe to a run's events after `lastEventId` (0 replays everything still buffered).
 *
 * A stale cursor is rejected with a JSON 409, thrown as `GatewayError` with code
 * `resync_required`. The generator returns when the Gateway ends the stream; that happens
 * after a terminal event, but also on shutdown, so callers decide whether to reconnect.
 */
export async function* readRunEvents(
  client: GatewayClient,
  runId: string,
  lastEventId: number,
  signal?: AbortSignal,
): AsyncGenerator<StreamItem, void, undefined> {
  const response = await client.send(`/v1/runs/${encodeURIComponent(runId)}/events`, {
    headers: { Accept: "text/event-stream", "Last-Event-ID": String(lastEventId) },
    signal,
  });
  if (response.body === null) {
    throw new ProtocolError("Event stream has no body");
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = new SseParser();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        return;
      }
      for (const frame of parser.push(value)) {
        if (frame.event === "resync_required") {
          yield { kind: "resync" };
          return;
        }
        yield { kind: "event", envelope: decodeEnvelope(frame) };
      }
    }
  } finally {
    // Cancelling releases the connection when the caller stops early or aborts.
    await reader.cancel().catch(() => undefined);
  }
}
