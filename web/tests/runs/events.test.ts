import { describe, expect, it } from "vitest";

import { ProtocolError } from "@/api/decode";
import { decodeRunEvent, isTerminalType } from "@/runs/events";

const RUN = "r".repeat(32);

function envelope(type: string, data: Record<string, unknown>, id = 4) {
  return { id, run_id: RUN, type, data };
}

describe("decodeRunEvent", () => {
  it.each([
    ["run.accepted", { status: "pending" }, { type: "run.accepted" }],
    ["run.started", { status: "running" }, { type: "run.started" }],
    ["run.stop_requested", { status: "running" }, { type: "run.stop_requested" }],
    ["run.committing", { status: "committing" }, { type: "run.committing" }],
    ["text.delta", { text: "Hi" }, { type: "text.delta", text: "Hi" }],
    ["tool.generating", { name: "lookup" }, { type: "tool.generating", name: "lookup" }],
    [
      "tool.started",
      { call_id: "c", name: "lookup", arguments: { q: 1 }, arguments_truncated: false },
      {
        type: "tool.started",
        callId: "c",
        name: "lookup",
        arguments: { q: 1 },
        argumentsTruncated: false,
      },
    ],
    [
      "tool.finished",
      { call_id: "c", name: "lookup", duration_s: 0.25, is_error: true },
      { type: "tool.finished", callId: "c", name: "lookup", durationS: 0.25, isError: true },
    ],
    [
      "retry.scheduled",
      { attempt: 1, max_attempts: 3, wait_s: 1.5, reason: "http 429" },
      { type: "retry.scheduled", attempt: 1, maxAttempts: 3, waitS: 1.5, reason: "http 429" },
    ],
    [
      "run.partial",
      {
        status: "partial",
        durable: true,
        revision: 2,
        error: null,
        reconciliation_required: false,
        text_truncated: true,
        finished_at_us: 9,
      },
      {
        type: "run.terminal",
        status: "partial",
        durable: true,
        revision: 2,
        error: null,
        reconciliationRequired: false,
        textTruncated: true,
        finishedAtUs: 9,
      },
    ],
  ])("decodes %s", (type, data, expected) => {
    expect(decodeRunEvent(envelope(type, data))).toEqual({ id: 4, runId: RUN, ...expected });
  });

  it("decodes events from a Gateway without the Web UI fields", () => {
    expect(decodeRunEvent(envelope("tool.started", { call_id: "c", name: "lookup" }))).toEqual({
      id: 4,
      runId: RUN,
      type: "tool.started",
      callId: "c",
      name: "lookup",
    });
    const stopped = decodeRunEvent(
      envelope("run.stopped", {
        status: "stopped",
        durable: false,
        revision: null,
        error: null,
        reconciliation_required: false,
      }),
    );
    expect(stopped).not.toHaveProperty("textTruncated");
    expect(stopped).not.toHaveProperty("finishedAtUs");
  });

  it("keeps an unknown event type with its data", () => {
    expect(decodeRunEvent(envelope("run.thinking", { depth: 2 }))).toEqual({
      id: 4,
      runId: RUN,
      type: "unknown",
      name: "run.thinking",
      data: { depth: 2 },
    });
  });

  it.each([
    ["text.delta", { text: 1 }],
    ["tool.started", { call_id: "c", name: "lookup", arguments: "{}" }],
    ["tool.finished", { call_id: "c", name: "lookup", duration_s: "1", is_error: false }],
    ["retry.scheduled", { attempt: 1.5, max_attempts: 3, wait_s: 1, reason: "x" }],
    [
      "run.completed",
      { status: "failed", durable: true, revision: 1, error: null, reconciliation_required: false },
    ],
    [
      "run.failed",
      {
        status: "failed",
        durable: "yes",
        revision: null,
        error: null,
        reconciliation_required: false,
      },
    ],
  ])("rejects a malformed %s", (type, data) => {
    expect(() => decodeRunEvent(envelope(type, data))).toThrow(ProtocolError);
  });

  it("knows the four terminal types", () => {
    expect(
      ["run.completed", "run.partial", "run.stopped", "run.failed"].every(isTerminalType),
    ).toBe(true);
    expect(isTerminalType("run.committing")).toBe(false);
  });
});
