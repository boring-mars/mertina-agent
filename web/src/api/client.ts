/**
 * REST client for the Gateway's Sessions and Runs APIs.
 *
 * Every method validates the response before returning it; a malformed body is a
 * {@link ProtocolError}, an error status a {@link GatewayError}, and a failed connection a
 * {@link GatewayUnreachableError}. `fetch` is injectable so tests replace the network,
 * not this module.
 */

import { gatewayError, GatewayUnreachableError } from "./errors";
import {
  decodeCommitReceipt,
  decodeMessagePage,
  decodeRunSnapshot,
  decodeSession,
  decodeSessionPage,
  type CommitReceipt,
  type MessagePage,
  type RunSnapshot,
  type Session,
  type SessionCursor,
  type SessionPage,
} from "./types";

export type Fetch = typeof globalThis.fetch;

export interface ClientOptions {
  /** Prefix for every path; empty for the same origin the UI was served from. */
  readonly baseUrl?: string;
  readonly fetch?: Fetch;
}

export interface ListSessionsOptions {
  readonly limit?: number;
  /** The previous page's `next_cursor`, passed back unchanged. */
  readonly cursor?: SessionCursor | null;
}

export interface ReadMessagesOptions {
  readonly after?: number;
  readonly limit?: number;
  /** The `through` of the first page, so later pages read the same committed history. */
  readonly through?: number;
}

interface RequestOptions {
  readonly method?: "GET" | "POST" | "PATCH" | "DELETE";
  readonly body?: unknown;
  readonly headers?: Readonly<Record<string, string>>;
  readonly signal?: AbortSignal | undefined;
}

export class GatewayClient {
  readonly baseUrl: string;
  readonly #fetch: Fetch;

  constructor(options: ClientOptions = {}) {
    this.baseUrl = options.baseUrl ?? "";
    // Bind so the browser's fetch keeps `window` as its receiver.
    this.#fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  /** Send a request and return the raw response once its status is a success. */
  async send(path: string, options: RequestOptions = {}): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
    const init: RequestInit = { method: options.method ?? "GET", headers };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (options.signal !== undefined) {
      init.signal = options.signal;
    }
    let response: Response;
    try {
      response = await this.#fetch(this.baseUrl + path, init);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw error;
      }
      throw new GatewayUnreachableError("The Gateway could not be reached", { cause: error });
    }
    if (!response.ok) {
      throw await gatewayError(response);
    }
    return response;
  }

  async #json<T>(path: string, decode: (value: unknown) => T, options?: RequestOptions) {
    const response = await this.send(path, options);
    return decode(await response.json());
  }

  listSessions(options: ListSessionsOptions = {}): Promise<SessionPage> {
    const query = new URLSearchParams({ order: "updated_desc" });
    if (options.limit !== undefined) {
      query.set("limit", String(options.limit));
    }
    for (const [key, value] of Object.entries(options.cursor ?? {})) {
      query.set(key, String(value));
    }
    return this.#json(`/api/sessions?${query.toString()}`, decodeSessionPage);
  }

  getSession(sessionId: string): Promise<Session> {
    return this.#json(`/api/sessions/${encodeURIComponent(sessionId)}`, decodeSession);
  }

  createSession(title?: string): Promise<Session> {
    return this.#json("/api/sessions", decodeSession, {
      method: "POST",
      body: title === undefined ? {} : { title },
    });
  }

  renameSession(sessionId: string, title: string, expectedVersion: number): Promise<Session> {
    return this.#json(`/api/sessions/${encodeURIComponent(sessionId)}`, decodeSession, {
      method: "PATCH",
      body: { title, expected_version: expectedVersion },
    });
  }

  async deleteSession(sessionId: string): Promise<void> {
    await this.send(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  }

  readMessages(sessionId: string, options: ReadMessagesOptions = {}): Promise<MessagePage> {
    const query = new URLSearchParams();
    for (const key of ["after", "limit", "through"] as const) {
      const value = options[key];
      if (value !== undefined) {
        query.set(key, String(value));
      }
    }
    const suffix = query.size === 0 ? "" : `?${query.toString()}`;
    return this.#json(
      `/api/sessions/${encodeURIComponent(sessionId)}/messages${suffix}`,
      decodeMessagePage,
    );
  }

  /**
   * Look up whether a run's turn was saved.
   *
   * `committed: false` is final. While the run may still commit, the Gateway answers
   * 409 `conflict` instead, which surfaces here as a {@link GatewayError}.
   */
  getCommit(sessionId: string, runId: string): Promise<CommitReceipt> {
    return this.#json(
      `/api/sessions/${encodeURIComponent(sessionId)}/commits/${encodeURIComponent(runId)}`,
      decodeCommitReceipt,
    );
  }

  startRun(sessionId: string, message: string): Promise<RunSnapshot> {
    return this.#json("/v1/runs", decodeRunSnapshot, {
      method: "POST",
      body: { session_id: sessionId, message },
    });
  }

  getRun(runId: string, signal?: AbortSignal): Promise<RunSnapshot> {
    return this.#json(`/v1/runs/${encodeURIComponent(runId)}`, decodeRunSnapshot, { signal });
  }

  stopRun(runId: string): Promise<RunSnapshot> {
    return this.#json(`/v1/runs/${encodeURIComponent(runId)}/stop`, decodeRunSnapshot, {
      method: "POST",
    });
  }
}
