/**
 * A scripted fake Gateway behind `fetch`, for tests that exercise the client end to end.
 *
 * Tests mock only the network boundary: each route answers from a queue of scripted
 * responses, and every request is recorded for assertions.
 */

import { vi } from "vitest";

import { GatewayClient } from "@/api/client";

export interface RecordedRequest {
  readonly method: string;
  readonly path: string;
  readonly headers: Headers;
  readonly body: unknown;
}

type Reply = Response | Error | (() => Response | Error);

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function errorEnvelope(status: number, code: string): Response {
  return json({ error: { code, message: `${code} message` } }, status);
}

/** An SSE body that delivers `chunks` one by one, then ends (or errors). */
export function eventStream(chunks: readonly string[], options: { readonly fail?: Error } = {}) {
  const encoder = new TextEncoder();
  let index = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      const chunk = chunks[index];
      index += 1;
      if (chunk !== undefined) {
        controller.enqueue(encoder.encode(chunk));
      } else if (options.fail !== undefined) {
        controller.error(options.fail);
      } else {
        controller.close();
      }
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

/** Encode envelopes the way the Gateway frames them. */
export function frames(
  envelopes: readonly { id: number; run_id: string; type: string; data: object }[],
): string {
  return envelopes
    .map(
      (envelope) =>
        `id: ${envelope.id}\nevent: ${envelope.type}\ndata: ${JSON.stringify(envelope)}\n\n`,
    )
    .join("");
}

export class FakeGateway {
  readonly requests: RecordedRequest[] = [];
  readonly #routes = new Map<string, Reply[]>();
  readonly fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(
      typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
      "http://gateway.test",
    );
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
    this.requests.push({
      method,
      path: url.pathname + url.search,
      headers: new Headers(init?.headers),
      body,
    });
    if (init?.signal?.aborted === true) {
      throw new DOMException("Aborted", "AbortError");
    }
    const key = `${method} ${url.pathname}`;
    const queue = this.#routes.get(key);
    const reply = queue?.shift();
    if (reply === undefined) {
      throw new Error(`Unscripted request: ${key}`);
    }
    const result = typeof reply === "function" ? reply() : reply;
    if (result instanceof Error) {
      throw result;
    }
    return Promise.resolve(result);
  });

  readonly client = new GatewayClient({ fetch: this.fetch });

  /** Queue replies for `METHOD /path` (path without query). */
  on(route: string, ...replies: Reply[]): this {
    const queue = this.#routes.get(route) ?? [];
    queue.push(...replies);
    this.#routes.set(route, queue);
    return this;
  }

  requestsTo(route: string): RecordedRequest[] {
    return this.requests.filter(
      (request) => `${request.method} ${request.path.split("?")[0]}` === route,
    );
  }
}
