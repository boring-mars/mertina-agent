/**
 * The run-event reducer: the one place that turns Gateway events into display state.
 *
 * It is a pure function. It reads no clock and sends no request: the caller passes the
 * time each event arrived. Components read the resulting state and never interpret
 * events themselves.
 */

import type { JsonRecord } from "@/api/decode";
import type { RunSnapshot, RunStatus, SnapshotToolCall, ToolCallStatus } from "@/api/types";

import type { RunEvent } from "./events";

/** What the run is doing now, for the progress strip. */
export type RunPhase = "queued" | "thinking" | "tools" | "writing" | "retrying" | "saving" | "done";

export interface ToolCallState {
  readonly callId: string;
  readonly name: string;
  readonly status: ToolCallStatus;
  readonly durationS: number | null;
  /** `null` when withheld for size; `undefined` when the Gateway did not send arguments. */
  readonly arguments: JsonRecord | null | undefined;
  readonly argumentsTruncated: boolean;
}

export interface RetryState {
  readonly attempt: number;
  readonly maxAttempts: number;
  readonly waitS: number;
  readonly reason: string;
  /** Client time (ms) the retry was announced; the countdown ends at `+ waitS`. */
  readonly scheduledAt: number;
}

/** One line in the inspector's event stream. */
export interface TimelineEntry {
  readonly eventId: number;
  /** The wire event type, including types this UI does not understand. */
  readonly type: string;
  readonly at: number;
}

export interface RunState {
  readonly runId: string;
  readonly sessionId: string;
  readonly status: RunStatus;
  readonly phase: RunPhase;
  readonly text: string;
  /**
   * Text of an attempt that failed and will be retried. It is shown faded until the
   * next attempt's first text arrives, so the answer does not flash empty.
   */
  readonly staleText: string | null;
  readonly textTruncated: boolean;
  readonly tools: readonly ToolCallState[];
  /** Name of the tool the model is still writing a call for. */
  readonly generatingTool: string | null;
  readonly retry: RetryState | null;
  readonly stopRequested: boolean;
  readonly terminal: boolean;
  readonly durable: boolean;
  readonly revision: number | null;
  readonly error: string | null;
  readonly reconciliationRequired: boolean;
  /** Highest event ID applied; events at or below it are duplicates from a reconnect. */
  readonly lastEventId: number;
  readonly acceptedAtUs: number | null;
  readonly finishedAtUs: number | null;
  readonly timeline: readonly TimelineEntry[];
}

/** Oldest timeline entries are dropped beyond this, so long runs stay bounded. */
export const MAX_TIMELINE = 500;

function phaseOf(status: RunStatus, tools: readonly ToolCallState[], text: string): RunPhase {
  switch (status) {
    case "pending":
      return "queued";
    case "committing":
      return "saving";
    case "running":
      if (tools.some((tool) => tool.status === "running")) {
        return "tools";
      }
      return text === "" ? "thinking" : "writing";
    default:
      return "done";
  }
}

function fromSnapshotTool(call: SnapshotToolCall): ToolCallState {
  return {
    callId: call.call_id,
    name: call.name,
    status: call.status,
    durationS: call.duration_s,
    arguments: call.arguments,
    argumentsTruncated: call.arguments_truncated,
  };
}

/** Start from the snapshot that `POST /v1/runs` or `GET /v1/runs/{id}` returned. */
export function initialRunState(snapshot: RunSnapshot): RunState {
  const tools = (snapshot.tool_calls ?? []).map(fromSnapshotTool);
  return {
    runId: snapshot.run_id,
    sessionId: snapshot.session_id,
    status: snapshot.status,
    phase: phaseOf(snapshot.status, tools, snapshot.text),
    text: snapshot.text,
    staleText: null,
    textTruncated: snapshot.text_truncated,
    tools,
    generatingTool: null,
    retry: null,
    stopRequested: snapshot.stop_requested,
    terminal: snapshot.terminal,
    durable: snapshot.durable,
    revision: snapshot.revision,
    error: snapshot.error,
    reconciliationRequired: snapshot.reconciliation_required,
    lastEventId: snapshot.last_event_id,
    acceptedAtUs: snapshot.accepted_at_us ?? null,
    finishedAtUs: snapshot.finished_at_us ?? null,
    timeline: [],
  };
}

/**
 * Replace event-derived state with a snapshot after the event buffer overflowed.
 *
 * The snapshot's text and tool calls are authoritative. A Gateway that predates
 * `tool_calls` in snapshots sends none, and then the cards already shown are kept rather
 * than wiped. The timeline is kept, so the inspector still shows what was received.
 */
export function applySnapshot(state: RunState, snapshot: RunSnapshot): RunState {
  if (snapshot.run_id !== state.runId) {
    return state;
  }
  const next = initialRunState(snapshot);
  const tools = snapshot.tool_calls === undefined ? state.tools : next.tools;
  // Only the next attempt's text clears a retry, so an empty snapshot during the
  // backoff keeps the faded draft and the countdown.
  const retrying = state.retry !== null && snapshot.text === "" && !snapshot.terminal;
  return {
    ...next,
    tools,
    phase: retrying ? "retrying" : phaseOf(next.status, tools, next.text),
    staleText: retrying ? state.staleText : null,
    retry: retrying ? state.retry : null,
    acceptedAtUs: next.acceptedAtUs ?? state.acceptedAtUs,
    timeline: state.timeline,
  };
}

function updateLastRunning(
  tools: readonly ToolCallState[],
  callId: string,
  update: (tool: ToolCallState) => ToolCallState,
): readonly ToolCallState[] {
  // A model may reuse a call ID later in the turn; the most recent running call wins,
  // matching how the Gateway builds snapshots.
  for (let index = tools.length - 1; index >= 0; index -= 1) {
    const tool = tools[index];
    if (tool !== undefined && tool.callId === callId && tool.status === "running") {
      return tools.map((item, position) => (position === index ? update(item) : item));
    }
  }
  return tools;
}

function apply(state: RunState, event: RunEvent, at: number): RunState {
  switch (event.type) {
    case "run.accepted":
      return { ...state, status: "pending", phase: "queued" };
    case "run.started":
      return {
        ...state,
        status: "running",
        phase: phaseOf("running", state.tools, state.text),
      };
    case "tool.generating":
      return { ...state, phase: "tools", generatingTool: event.name };
    case "tool.started":
      return {
        ...state,
        phase: "tools",
        generatingTool: null,
        tools: [
          ...state.tools,
          {
            callId: event.callId,
            name: event.name,
            status: "running",
            durationS: null,
            arguments: event.arguments,
            argumentsTruncated: event.argumentsTruncated ?? false,
          },
        ],
      };
    case "tool.finished": {
      const tools = updateLastRunning(state.tools, event.callId, (tool) => ({
        ...tool,
        status: event.isError ? "error" : "ok",
        durationS: event.durationS,
      }));
      const phase =
        state.phase === "tools" ? phaseOf(state.status, tools, state.text) : state.phase;
      return { ...state, tools, phase };
    }
    case "text.delta":
      return {
        ...state,
        phase: "writing",
        text: state.text + event.text,
        staleText: null,
        retry: null,
        generatingTool: null,
      };
    case "retry.scheduled":
      return {
        ...state,
        phase: "retrying",
        // The retry starts the answer over; keep the old text only as a faded draft.
        staleText: state.text === "" ? state.staleText : state.text,
        text: "",
        textTruncated: false,
        generatingTool: null,
        retry: {
          attempt: event.attempt,
          maxAttempts: event.maxAttempts,
          waitS: event.waitS,
          reason: event.reason,
          scheduledAt: at,
        },
      };
    case "run.stop_requested":
      return { ...state, stopRequested: true };
    case "run.committing":
      return {
        ...state,
        status: "committing",
        phase: "saving",
        retry: null,
        generatingTool: null,
      };
    case "run.terminal":
      return {
        ...state,
        status: event.status,
        phase: "done",
        terminal: true,
        durable: event.durable,
        revision: event.revision,
        error: event.error,
        reconciliationRequired: event.reconciliationRequired,
        textTruncated: event.textTruncated ?? state.textTruncated,
        finishedAtUs: event.finishedAtUs ?? state.finishedAtUs,
        staleText: null,
        retry: null,
        generatingTool: null,
      };
    case "unknown":
      return state;
  }
}

/**
 * Apply one event that arrived at client time `at` (milliseconds).
 *
 * Events for another run, events already applied (a reconnect replays from the cursor)
 * and events after the terminal one leave the state unchanged. Unknown event types only
 * add a timeline entry.
 */
export function reduceRun(state: RunState, event: RunEvent, at: number): RunState {
  if (event.runId !== state.runId || event.id <= state.lastEventId || state.terminal) {
    return state;
  }
  const next = apply(state, event, at);
  const wireType = event.type === "unknown" ? event.name : wireTypeOf(event);
  const timeline = [...state.timeline, { eventId: event.id, type: wireType, at }];
  return {
    ...next,
    lastEventId: event.id,
    timeline: timeline.length > MAX_TIMELINE ? timeline.slice(-MAX_TIMELINE) : timeline,
  };
}

function wireTypeOf(event: Exclude<RunEvent, { type: "unknown" }>): string {
  return event.type === "run.terminal" ? `run.${event.status}` : event.type;
}
