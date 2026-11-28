#!/usr/bin/env node
/**
 * Record one run's event sequence from a live Gateway as a test fixture.
 *
 * Usage:
 *   node web/scripts/recordRun.mjs --name tool --message "hello [tool]" [--stop-on text.delta]
 *     [--gateway http://127.0.0.1:8765] [--out web/tests/fixtures/runs]
 *
 * The fixture holds the snapshot `POST /v1/runs` returned, every event with the time it
 * arrived (milliseconds after the run was accepted), and the final `GET /v1/runs/{id}`
 * snapshot. Run and session IDs are replaced with fixed placeholders so re-recording
 * gives stable diffs. Record against the dev/e2e stub model only: fixtures are committed,
 * so they must not contain real model output, keys or personal data.
 */

import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { parseArgs } from "node:util";

const RUN_ID = "0".repeat(31) + "1";
const SESSION_ID = "0".repeat(31) + "2";
const TERMINAL = new Set(["run.completed", "run.partial", "run.stopped", "run.failed"]);

const { values } = parseArgs({
  options: {
    name: { type: "string" },
    message: { type: "string" },
    description: { type: "string", default: "" },
    "stop-on": { type: "string" },
    gateway: { type: "string", default: "http://127.0.0.1:8765" },
    out: { type: "string", default: "web/tests/fixtures/runs" },
  },
});
if (values.name === undefined || values.message === undefined) {
  console.error("--name and --message are required");
  process.exit(2);
}
const gateway = values.gateway;

async function request(path, init = {}) {
  const response = await fetch(gateway + path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) {
    throw new Error(`${init.method ?? "GET"} ${path}: HTTP ${response.status}`);
  }
  return response;
}

function anonymize(value, runId, sessionId) {
  const text = JSON.stringify(value).replaceAll(runId, RUN_ID).replaceAll(sessionId, SESSION_ID);
  return JSON.parse(text);
}

const session = await (
  await request("/api/sessions", { method: "POST", body: JSON.stringify({ title: values.name }) })
).json();
const started = Date.now();
const start = await (
  await request("/v1/runs", {
    method: "POST",
    body: JSON.stringify({ session_id: session.id, message: values.message }),
  })
).json();

const events = [];
let stopped = false;
const stream = await request(`/v1/runs/${start.run_id}/events`, {
  headers: { Accept: "text/event-stream", "Last-Event-ID": "0" },
});
const decoder = new TextDecoder();
let buffer = "";
let done = false;
for await (const chunk of stream.body) {
  buffer += decoder.decode(chunk, { stream: true });
  let boundary;
  while (!done && (boundary = buffer.indexOf("\n\n")) !== -1) {
    const frame = buffer.slice(0, boundary);
    buffer = buffer.slice(boundary + 2);
    const data = frame
      .split("\n")
      .filter((line) => line.startsWith("data: "))
      .map((line) => line.slice(6))
      .join("\n");
    if (data === "") {
      continue;
    }
    const envelope = JSON.parse(data);
    events.push({ at: Date.now() - started, envelope });
    if (!stopped && values["stop-on"] === envelope.type) {
      stopped = true;
      await request(`/v1/runs/${start.run_id}/stop`, { method: "POST" });
    }
    done = TERMINAL.has(envelope.type);
  }
  if (done) {
    break;
  }
}
const final = await (await request(`/v1/runs/${start.run_id}`)).json();

const fixture = anonymize(
  {
    description: values.description,
    recorded_with: "dev/e2e stub model and serve_gateway.py",
    message: values.message,
    start,
    events,
    final,
  },
  start.run_id,
  session.id,
);
await mkdir(values.out, { recursive: true });
const path = join(values.out, `${values.name}.json`);
await writeFile(path, JSON.stringify(fixture, null, 2) + "\n");
console.log(`${path}: ${events.length} events, ${final.status}`);
