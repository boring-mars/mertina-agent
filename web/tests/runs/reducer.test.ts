import { describe, expect, it } from "vitest";

import type { RunSnapshot, TerminalError } from "@/api/types";
import type { RunEvent } from "@/runs/events";
import {
  applySnapshot,
  initialRunState,
  MAX_TIMELINE,
  reduceRun,
  type RunState,
  type ToolCallState,
} from "@/runs/reducer";

import { runFixture, runFixtures } from "../fixtures/runs";

const RUN = "r".repeat(32);
const SESSION = "s".repeat(32);

function snapshot(overrides: Partial<RunSnapshot> = {}): RunSnapshot {
  return {
    run_id: RUN,
    session_id: SESSION,
    status: "pending",
    text: "",
    text_truncated: false,
    error: null,
    durable: false,
    terminal: false,
    stop_requested: false,
    reconciliation_required: false,
    revision: null,
    last_event_id: 0,
    accepted_at_us: 1_000,
    finished_at_us: null,
    tool_calls: [],
    ...overrides,
  };
}

type EventBody = RunEvent extends infer E
  ? E extends RunEvent
    ? Omit<E, "id" | "runId">
    : never
  : never;

/** Apply bodies as consecutive events 1..n, at times 10, 20, ... */
function run(bodies: readonly EventBody[], state: RunState = initialRunState(snapshot())) {
  return bodies.reduce(
    (current, body, index) =>
      reduceRun(current, { ...body, id: current.lastEventId + 1, runId: RUN }, (index + 1) * 10),
    state,
  );
}

function terminal(
  overrides: Partial<Extract<EventBody, { type: "run.terminal" }>> = {},
): EventBody {
  return {
    type: "run.terminal",
    status: "completed",
    durable: true,
    revision: 1,
    error: null,
    reconciliationRequired: false,
    ...overrides,
  };
}

function toolsOf(snap: RunSnapshot): ToolCallState[] {
  return (snap.tool_calls ?? []).map((call) => ({
    callId: call.call_id,
    name: call.name,
    status: call.status,
    durationS: call.duration_s,
    arguments: call.arguments,
    argumentsTruncated: call.arguments_truncated,
  }));
}

describe("recorded runs", () => {
  it("has fixtures to check", () => {
    expect(runFixtures.size).toBeGreaterThanOrEqual(8);
  });

  // The final text is the turn's saved answer, which can differ from what streamed: a
  // failed or early-stopped turn saves an explanation, and a stopped one is trimmed. The
  // terminal event does not carry it, so the UI reads the snapshot (or messages) after it.
  const streamedTextIsFinal = new Set(["plain", "tool", "parallel", "tool-error", "retry"]);

  function replay(name: string): RunState {
    const fixture = runFixture(name);
    let state = initialRunState(fixture.start);
    for (const { at, event } of fixture.events) {
      state = reduceRun(state, event, at);
    }
    return state;
  }

  for (const fixture of runFixtures.values()) {
    it(`${fixture.name}: events reproduce the Gateway's final snapshot`, () => {
      const state = replay(fixture.name);
      const { final } = fixture;
      expect(state).toMatchObject({
        status: final.status,
        textTruncated: final.text_truncated,
        terminal: true,
        durable: final.durable,
        revision: final.revision,
        error: final.error,
        reconciliationRequired: final.reconciliation_required,
        lastEventId: final.last_event_id,
        finishedAtUs: final.finished_at_us,
        phase: "done",
        staleText: null,
        retry: null,
      });
      if (streamedTextIsFinal.has(fixture.name)) {
        expect(state.text).toBe(final.text);
      }
      expect(state.tools).toEqual(toolsOf(final));
      // The accepted event is already part of the snapshot `POST /v1/runs` returns.
      expect(state.timeline.map((entry) => entry.type)).toEqual(
        fixture.envelopes
          .filter((envelope) => envelope.id > fixture.start.last_event_id)
          .map((envelope) => envelope.type),
      );
    });

    it(`${fixture.name}: the final snapshot settles the text and matches a fresh load`, () => {
      const settled = applySnapshot(replay(fixture.name), fixture.final);
      expect(settled.text).toBe(fixture.final.text);
      const fresh = applySnapshot(initialRunState(fixture.start), fixture.final);
      expect({ ...settled, timeline: [] }).toEqual({ ...fresh, timeline: [] });
    });
  }

  it.each([
    ["plain", ["queued", "thinking", "writing", "writing", "writing", "writing", "saving", "done"]],
    [
      "tool",
      ["queued", "thinking", "tools", "tools", "thinking", "writing", "writing", "saving", "done"],
    ],
    [
      "parallel",
      [
        "queued",
        "thinking",
        "tools",
        "tools",
        "tools",
        "thinking",
        "tools",
        "thinking",
        "writing",
        "writing",
        "saving",
        "done",
      ],
    ],
    [
      "retry",
      [
        "queued",
        "thinking",
        "retrying",
        "writing",
        "writing",
        "writing",
        "writing",
        "saving",
        "done",
      ],
    ],
    ["model-failed", ["queued", "thinking", "retrying", "retrying", "saving", "done"]],
    ["stopped", ["queued", "thinking", "writing", "writing", "saving", "done"]],
  ])("%s: phases after each event", (name, phases) => {
    const fixture = runFixture(name);
    let state = initialRunState(fixture.start);
    const trail: string[] = [];
    for (const { at, event } of fixture.events) {
      state = reduceRun(state, event, at);
      trail.push(state.phase);
    }
    expect(trail).toEqual(phases);
  });

  it("tool-error: a failed tool stays marked as an error and the run still completes", () => {
    const fixture = runFixture("tool-error");
    let state = initialRunState(fixture.start);
    for (const { at, event } of fixture.events) {
      state = reduceRun(state, event, at);
    }
    expect(state.status).toBe("completed");
    expect(state.tools).toMatchObject([{ status: "error", arguments: { fail: true } }]);
  });

  it("stopped: the stop request is visible before the terminal event", () => {
    const fixture = runFixture("stopped");
    let state = initialRunState(fixture.start);
    for (const { at, event } of fixture.events) {
      state = reduceRun(state, event, at);
      if (event.type === "run.stop_requested") {
        expect(state).toMatchObject({ stopRequested: true, terminal: false });
      }
    }
    expect(state).toMatchObject({ status: "stopped", durable: true, stopRequested: true });
  });
});

describe("reduceRun", () => {
  it("starts from the accepted snapshot", () => {
    const state = initialRunState(snapshot());
    expect(state).toMatchObject({
      runId: RUN,
      sessionId: SESSION,
      status: "pending",
      phase: "queued",
      acceptedAtUs: 1_000,
      lastEventId: 0,
    });
  });

  it("ignores events already applied, from another run, or after the terminal event", () => {
    const state = run([{ type: "run.started" }, { type: "text.delta", text: "Hi" }]);
    const replay = reduceRun(state, { type: "text.delta", text: "Hi", id: 2, runId: RUN }, 99);
    expect(replay).toBe(state);
    const older = reduceRun(state, { type: "text.delta", text: "x", id: 1, runId: RUN }, 99);
    expect(older).toBe(state);
    const other = reduceRun(
      state,
      { type: "text.delta", text: "x", id: 3, runId: "o".repeat(32) },
      99,
    );
    expect(other).toBe(state);
    const done = run([terminal()], state);
    const after = reduceRun(done, { type: "text.delta", text: "late", id: 99, runId: RUN }, 99);
    expect(after).toBe(done);
  });

  it("keeps unknown event types in the timeline without changing state", () => {
    const before = run([{ type: "run.started" }]);
    const after = run([{ type: "unknown", name: "run.thinking", data: { depth: 2 } }], before);
    expect(after).toEqual({
      ...before,
      lastEventId: 2,
      timeline: [...before.timeline, { eventId: 2, type: "run.thinking", at: 10 }],
    });
  });

  it("names the generating tool until its call starts", () => {
    const generating = run([{ type: "run.started" }, { type: "tool.generating", name: "lookup" }]);
    expect(generating).toMatchObject({ phase: "tools", generatingTool: "lookup" });
    const started = run(
      [
        {
          type: "tool.started",
          callId: "c1",
          name: "lookup",
          arguments: { q: "x" },
          argumentsTruncated: false,
        },
      ],
      generating,
    );
    expect(started.generatingTool).toBeNull();
  });

  it("stays in the tools phase until every parallel call finishes", () => {
    const state = run([
      { type: "run.started" },
      {
        type: "tool.started",
        callId: "a",
        name: "lookup",
        arguments: {},
        argumentsTruncated: false,
      },
      {
        type: "tool.started",
        callId: "b",
        name: "lookup",
        arguments: {},
        argumentsTruncated: false,
      },
      { type: "tool.finished", callId: "a", name: "lookup", durationS: 0.2, isError: false },
    ]);
    expect(state.phase).toBe("tools");
    const done = run(
      [{ type: "tool.finished", callId: "b", name: "lookup", durationS: 0.4, isError: true }],
      state,
    );
    expect(done.phase).toBe("thinking");
    expect(done.tools.map((tool) => [tool.callId, tool.status, tool.durationS])).toEqual([
      ["a", "ok", 0.2],
      ["b", "error", 0.4],
    ]);
  });

  it("finishes the most recent running call when a call ID is reused", () => {
    const state = run([
      { type: "run.started" },
      {
        type: "tool.started",
        callId: "c",
        name: "lookup",
        arguments: { n: 1 },
        argumentsTruncated: false,
      },
      { type: "tool.finished", callId: "c", name: "lookup", durationS: 0.1, isError: false },
      {
        type: "tool.started",
        callId: "c",
        name: "lookup",
        arguments: { n: 2 },
        argumentsTruncated: false,
      },
      { type: "tool.finished", callId: "c", name: "lookup", durationS: 0.3, isError: true },
    ]);
    expect(state.tools.map((tool) => [tool.arguments, tool.status])).toEqual([
      [{ n: 1 }, "ok"],
      [{ n: 2 }, "error"],
    ]);
  });

  it("ignores a finish for a call it never saw", () => {
    const state = run([
      { type: "run.started" },
      { type: "tool.finished", callId: "ghost", name: "lookup", durationS: 0.1, isError: false },
    ]);
    expect(state.tools).toEqual([]);
  });

  it("marks arguments withheld for size, and missing from an older Gateway", () => {
    const state = run([
      {
        type: "tool.started",
        callId: "big",
        name: "lookup",
        arguments: null,
        argumentsTruncated: true,
      },
      { type: "tool.started", callId: "old", name: "lookup" },
    ]);
    expect(state.tools.map((tool) => [tool.arguments, tool.argumentsTruncated])).toEqual([
      [null, true],
      [undefined, false],
    ]);
  });

  it("fades the failed attempt's text until the retry's first text arrives", () => {
    const retrying = run([
      { type: "run.started" },
      { type: "text.delta", text: "Draft" },
      { type: "retry.scheduled", attempt: 1, maxAttempts: 3, waitS: 2, reason: "http 503" },
    ]);
    expect(retrying).toMatchObject({
      phase: "retrying",
      text: "",
      staleText: "Draft",
      retry: { attempt: 1, maxAttempts: 3, waitS: 2, reason: "http 503", scheduledAt: 30 },
    });
    // A second failure before any new text keeps the original draft.
    const again = run(
      [{ type: "retry.scheduled", attempt: 2, maxAttempts: 3, waitS: 4, reason: "http 503" }],
      retrying,
    );
    expect(again).toMatchObject({ staleText: "Draft", retry: { attempt: 2, scheduledAt: 10 } });
    const resumed = run([{ type: "text.delta", text: "New" }], again);
    expect(resumed).toMatchObject({ phase: "writing", text: "New", staleText: null, retry: null });
  });

  it("resets the truncation flag when a retry starts the answer over", () => {
    const state = initialRunState(snapshot({ status: "running", text: "x", text_truncated: true }));
    const retried = run(
      [{ type: "retry.scheduled", attempt: 1, maxAttempts: 2, waitS: 1, reason: "http 429" }],
      state,
    );
    expect(retried.textTruncated).toBe(false);
  });

  it("clears the retry countdown once the run starts saving", () => {
    const state = run([
      { type: "run.started" },
      { type: "retry.scheduled", attempt: 1, maxAttempts: 2, waitS: 1, reason: "http 503" },
      { type: "run.committing" },
    ]);
    expect(state).toMatchObject({ status: "committing", phase: "saving", retry: null });
  });

  it.each<[string, Partial<Extract<EventBody, { type: "run.terminal" }>>, Partial<RunState>]>([
    ["completed", {}, { status: "completed", durable: true, revision: 1, error: null }],
    ["partial", { status: "partial" }, { status: "partial", durable: true }],
    ["stopped and saved", { status: "stopped" }, { status: "stopped", durable: true }],
    [
      "stopped before anything was written",
      { status: "stopped", durable: false, revision: null },
      { status: "stopped", durable: false, revision: null },
    ],
    ...(
      [
        ["model_failed", true, false],
        ["run_failed", false, false],
        ["history_conflict", false, false],
        ["storage_unconfirmed", false, true],
        ["shutdown_cancelled", false, true],
      ] as const satisfies readonly (readonly [TerminalError, boolean, boolean])[]
    ).map(
      ([error, durable, reconciliationRequired]) =>
        [
          `failed with ${error}`,
          {
            status: "failed",
            error,
            durable,
            reconciliationRequired,
            revision: durable ? 1 : null,
          },
          { status: "failed", error, durable, reconciliationRequired },
        ] as [string, Partial<Extract<EventBody, { type: "run.terminal" }>>, Partial<RunState>],
    ),
    [
      "an error code from a newer Gateway",
      { status: "failed", error: "quota_exceeded", durable: false },
      { error: "quota_exceeded" },
    ],
  ])("ends the run: %s", (_name, event, expected) => {
    const state = run([
      { type: "run.started" },
      { type: "text.delta", text: "Hi" },
      terminal(event),
    ]);
    expect(state).toMatchObject({ ...expected, terminal: true, phase: "done", text: "Hi" });
  });

  it("takes truncation and finish time from the terminal event, or keeps them for an older Gateway", () => {
    const state = run([terminal({ textTruncated: true, finishedAtUs: 5_000 })]);
    expect(state).toMatchObject({ textTruncated: true, finishedAtUs: 5_000 });
    const legacy = run(
      [terminal()],
      initialRunState(snapshot({ status: "running", text_truncated: true })),
    );
    expect(legacy).toMatchObject({ textTruncated: true, finishedAtUs: null });
  });

  it("bounds the timeline", () => {
    const deltas: EventBody[] = Array.from({ length: MAX_TIMELINE + 5 }, () => ({
      type: "text.delta",
      text: "a",
    }));
    const state = run(deltas);
    expect(state.timeline).toHaveLength(MAX_TIMELINE);
    expect(state.timeline[0]?.eventId).toBe(6);
    expect(state.text).toHaveLength(MAX_TIMELINE + 5);
  });

  it("does not mutate the previous state", () => {
    const before = run([{ type: "run.started" }]);
    const frozen = structuredClone(before);
    run(
      [
        {
          type: "tool.started",
          callId: "c",
          name: "lookup",
          arguments: {},
          argumentsTruncated: false,
        },
      ],
      before,
    );
    expect(before).toEqual(frozen);
  });
});

describe("applySnapshot", () => {
  it("rebuilds text and tool cards after the event buffer overflowed", () => {
    const state = run([
      { type: "run.started" },
      {
        type: "tool.started",
        callId: "a",
        name: "lookup",
        arguments: {},
        argumentsTruncated: false,
      },
    ]);
    const resynced = applySnapshot(
      state,
      snapshot({
        status: "running",
        text: "Long answer",
        last_event_id: 900,
        tool_calls: [
          {
            call_id: "a",
            name: "lookup",
            status: "ok",
            duration_s: 0.5,
            is_error: false,
            arguments: {},
            arguments_truncated: false,
          },
          {
            call_id: "b",
            name: "lookup",
            status: "running",
            duration_s: null,
            is_error: false,
            arguments: null,
            arguments_truncated: true,
          },
        ],
      }),
    );
    expect(resynced).toMatchObject({ text: "Long answer", lastEventId: 900, phase: "tools" });
    expect(resynced.tools.map((tool) => [tool.callId, tool.status])).toEqual([
      ["a", "ok"],
      ["b", "running"],
    ]);
    expect(resynced.timeline).toBe(state.timeline);
  });

  it("keeps the tool cards it has when an older Gateway's snapshot has none", () => {
    const state = run([
      { type: "run.started" },
      {
        type: "tool.started",
        callId: "a",
        name: "lookup",
        arguments: {},
        argumentsTruncated: false,
      },
    ]);
    const {
      tool_calls: _ignored,
      accepted_at_us: _alsoIgnored,
      ...legacy
    } = snapshot({ status: "running" });
    const resynced = applySnapshot(state, legacy);
    expect(resynced.tools).toBe(state.tools);
    expect(resynced.acceptedAtUs).toBe(1_000);
  });

  it("keeps the faded draft and countdown while the retry has produced no text", () => {
    const state = run([
      { type: "run.started" },
      { type: "text.delta", text: "Draft" },
      { type: "retry.scheduled", attempt: 1, maxAttempts: 3, waitS: 2, reason: "http 503" },
    ]);
    const resynced = applySnapshot(state, snapshot({ status: "running", last_event_id: 300 }));
    expect(resynced).toMatchObject({ phase: "retrying", staleText: "Draft", retry: state.retry });
    const moved = applySnapshot(
      state,
      snapshot({ status: "running", text: "New", last_event_id: 301 }),
    );
    expect(moved).toMatchObject({ phase: "writing", staleText: null, retry: null });
  });

  it("ignores a snapshot of another run", () => {
    const state = run([{ type: "run.started" }]);
    expect(applySnapshot(state, snapshot({ run_id: "o".repeat(32) }))).toBe(state);
  });
});
