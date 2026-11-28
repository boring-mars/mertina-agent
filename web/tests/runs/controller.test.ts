import { describe, expect, it, vi } from "vitest";

import { GatewayError, GatewayUnreachableError } from "@/api/errors";
import type { RunSnapshot } from "@/api/types";
import {
  backoffMs,
  followRun,
  MAX_BACKOFF_MS,
  sleep,
  type ConnectionState,
} from "@/runs/controller";
import type { RunEvent } from "@/runs/events";

import { errorEnvelope, eventStream, FakeGateway, frames, json } from "../gateway";

const RUN = "r".repeat(32);
const EVENTS = `GET /v1/runs/${RUN}/events`;
const SNAPSHOT = `GET /v1/runs/${RUN}`;

const accepted = { id: 1, run_id: RUN, type: "run.accepted", data: { status: "pending" } };
const started = { id: 2, run_id: RUN, type: "run.started", data: { status: "running" } };
const delta = (id: number, text: string) => ({
  id,
  run_id: RUN,
  type: "text.delta",
  data: { text },
});
const completed = (id: number) => ({
  id,
  run_id: RUN,
  type: "run.completed",
  data: {
    status: "completed",
    durable: true,
    revision: 1,
    error: null,
    reconciliation_required: false,
  },
});

function snapshot(overrides: Partial<RunSnapshot> = {}) {
  return {
    run_id: RUN,
    session_id: "s".repeat(32),
    status: "running",
    text: "Hello world",
    text_truncated: false,
    error: null,
    durable: false,
    terminal: false,
    stop_requested: false,
    reconciliation_required: false,
    revision: null,
    last_event_id: 40,
    tool_calls: [],
    ...overrides,
  };
}

function follow(gateway: FakeGateway, after = 0, signal?: AbortSignal) {
  const events: RunEvent[] = [];
  const snapshots: RunSnapshot[] = [];
  const connection: ConnectionState[] = [];
  const waits: number[] = [];
  const result = followRun({
    client: gateway.client,
    runId: RUN,
    after,
    onEvent: (event) => events.push(event),
    onSnapshot: (value) => snapshots.push(value),
    onConnection: (state) => connection.push(state),
    sleep: (ms) => {
      waits.push(ms);
      return Promise.resolve();
    },
    ...(signal === undefined ? {} : { signal }),
  });
  return { result, events, snapshots, connection, waits };
}

function cursors(gateway: FakeGateway): (string | null)[] {
  return gateway.requestsTo(EVENTS).map((request) => request.headers.get("Last-Event-ID"));
}

describe("followRun", () => {
  it("delivers every event until the terminal one", async () => {
    const gateway = new FakeGateway().on(
      EVENTS,
      eventStream([frames([accepted, started, delta(3, "Hi"), completed(4)])]),
    );
    const run = follow(gateway);
    expect(await run.result).toBe("terminal");
    expect(run.events.map((event) => event.id)).toEqual([1, 2, 3, 4]);
    expect(run.connection[0]).toBe("connecting");
    expect(run.connection).toContain("live");
    expect(cursors(gateway)).toEqual(["0"]);
  });

  it("reconnects from the last event after the stream ends early", async () => {
    const gateway = new FakeGateway().on(
      EVENTS,
      eventStream([frames([accepted, started])]),
      eventStream([frames([delta(3, "Hi"), completed(4)])]),
    );
    const run = follow(gateway);
    expect(await run.result).toBe("terminal");
    expect(cursors(gateway)).toEqual(["0", "2"]);
    expect(run.waits).toEqual([500]);
    expect(run.connection).toContain("reconnecting");
  });

  it("reconnects after the connection breaks in the middle of a stream", async () => {
    const gateway = new FakeGateway().on(
      EVENTS,
      eventStream([frames([accepted, started])], { fail: new TypeError("network error") }),
      eventStream([frames([completed(3)])]),
    );
    const run = follow(gateway);
    expect(await run.result).toBe("terminal");
    expect(cursors(gateway)).toEqual(["0", "2"]);
  });

  it("skips events it already has when the Gateway replays from the cursor", async () => {
    const gateway = new FakeGateway().on(
      EVENTS,
      eventStream([frames([started, delta(3, "Hi"), completed(4)])]),
    );
    const run = follow(gateway, 2);
    expect(await run.result).toBe("terminal");
    expect(run.events.map((event) => event.id)).toEqual([3, 4]);
  });

  it("backs off exponentially up to the cap while the Gateway is unreachable", async () => {
    const down = new GatewayUnreachableError("down");
    const gateway = new FakeGateway().on(
      EVENTS,
      ...Array.from({ length: 7 }, () => down),
      errorEnvelope(503, "unavailable"),
      eventStream([frames([completed(1)])]),
    );
    const run = follow(gateway);
    expect(await run.result).toBe("terminal");
    expect(run.waits).toEqual([500, 1000, 2000, 4000, 8000, 10_000, 10_000, 10_000]);
  });

  it("resets the backoff once events flow again", async () => {
    const down = new GatewayUnreachableError("down");
    const gateway = new FakeGateway().on(
      EVENTS,
      down,
      down,
      eventStream([frames([accepted])]),
      down,
      eventStream([frames([completed(2)])]),
    );
    const run = follow(gateway);
    expect(await run.result).toBe("terminal");
    // The stream that delivered an event restarts the sequence at the first delay.
    expect(run.waits).toEqual([500, 1000, 500, 1000]);
  });

  it("resynchronizes from a snapshot when the cursor is no longer buffered", async () => {
    const gateway = new FakeGateway()
      .on(
        EVENTS,
        errorEnvelope(409, "resync_required"),
        eventStream([frames([delta(41, "!"), completed(42)])]),
      )
      .on(SNAPSHOT, json(snapshot()));
    const run = follow(gateway, 5);
    expect(await run.result).toBe("terminal");
    expect(run.snapshots.map((value) => value.text)).toEqual(["Hello world"]);
    expect(cursors(gateway)).toEqual(["5", "40"]);
    expect(run.events.map((event) => event.id)).toEqual([41, 42]);
    // Resynchronizing is not a failure, so it does not wait.
    expect(run.waits).toEqual([]);
  });

  it("resynchronizes when the Gateway drops a slow subscriber mid-stream", async () => {
    const gateway = new FakeGateway()
      .on(
        EVENTS,
        eventStream([
          frames([accepted, started]),
          'event: resync_required\ndata: {"code":"resync_required"}\n\n',
        ]),
        eventStream([frames([completed(41)])]),
      )
      .on(SNAPSHOT, json(snapshot()));
    const run = follow(gateway);
    expect(await run.result).toBe("terminal");
    expect(cursors(gateway)).toEqual(["0", "40"]);
    expect(run.snapshots).toHaveLength(1);
  });

  it("stops after resynchronizing when the snapshot is already terminal", async () => {
    const gateway = new FakeGateway()
      .on(EVENTS, errorEnvelope(409, "resync_required"))
      .on(
        SNAPSHOT,
        json(snapshot({ status: "completed", terminal: true, durable: true, revision: 1 })),
      );
    const run = follow(gateway, 5);
    expect(await run.result).toBe("terminal");
    expect(gateway.requestsTo(EVENTS)).toHaveLength(1);
  });

  it.each([
    ["subscribing", new FakeGateway().on(EVENTS, errorEnvelope(404, "not_found"))],
    [
      "reading the snapshot",
      new FakeGateway()
        .on(EVENTS, errorEnvelope(409, "resync_required"))
        .on(SNAPSHOT, errorEnvelope(404, "not_found")),
    ],
  ])("reports an expired run when the Gateway forgets it while %s", async (_name, gateway) => {
    expect(await follow(gateway).result).toBe("expired");
  });

  it("retries a snapshot read that fails while resynchronizing", async () => {
    const gateway = new FakeGateway()
      .on(EVENTS, errorEnvelope(409, "resync_required"), errorEnvelope(409, "resync_required"))
      .on(
        SNAPSHOT,
        new GatewayUnreachableError("down"),
        json(snapshot({ terminal: true, status: "stopped" })),
      );
    const run = follow(gateway, 5);
    expect(await run.result).toBe("terminal");
    expect(run.waits).toEqual([500]);
  });

  it.each([
    [400, "invalid_request"],
    [403, "forbidden_origin"],
  ])("gives up on %i %s, which retrying cannot fix", async (status, code) => {
    const gateway = new FakeGateway().on(EVENTS, errorEnvelope(status, code));
    const error = await follow(gateway).result.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(GatewayError);
    expect(error).toMatchObject({ status, code });
  });

  it("stops when aborted during the backoff", async () => {
    const controller = new AbortController();
    const gateway = new FakeGateway().on(EVENTS, new GatewayUnreachableError("down"));
    const result = followRun({
      client: gateway.client,
      runId: RUN,
      after: 0,
      onEvent: vi.fn(),
      onSnapshot: vi.fn(),
      signal: controller.signal,
      sleep: (ms, signal) => {
        controller.abort();
        return sleep(ms, signal);
      },
    });
    expect(await result).toBe("aborted");
  });

  it("stops when aborted while a request is in flight", async () => {
    const controller = new AbortController();
    const gateway = new FakeGateway().on(EVENTS, () => {
      controller.abort();
      return new DOMException("Aborted", "AbortError");
    });
    expect(await follow(gateway, 0, controller.signal).result).toBe("aborted");
  });
});

describe("backoff helpers", () => {
  it("doubles and caps the delay", () => {
    expect(
      [0, 1, 2, 3, 4, 5, 10].map((attempt) => backoffMs(attempt, 500, MAX_BACKOFF_MS)),
    ).toEqual([500, 1000, 2000, 4000, 8000, 10_000, 10_000]);
  });

  it("sleeps for the given time and can be aborted", async () => {
    vi.useFakeTimers();
    try {
      const done = vi.fn();
      void sleep(1000).then(done);
      await vi.advanceTimersByTimeAsync(999);
      expect(done).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(1);
      expect(done).toHaveBeenCalled();
      const controller = new AbortController();
      const aborted = sleep(1000, controller.signal);
      controller.abort(new Error("stop"));
      await expect(aborted).rejects.toThrow("stop");
      await expect(sleep(1, controller.signal)).rejects.toThrow("stop");
    } finally {
      vi.useRealTimers();
    }
  });
});
