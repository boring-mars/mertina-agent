import { describe, expect, it } from "vitest";

import { GatewayClient } from "@/api/client";
import { ProtocolError } from "@/api/decode";
import { GatewayError, GatewayUnreachableError } from "@/api/errors";

import { errorEnvelope, FakeGateway, json } from "../gateway";

const SESSION_ID = "s".repeat(32);
const RUN_ID = "r".repeat(32);

const session = {
  id: SESSION_ID,
  title: "Plans",
  created_at_us: 1,
  updated_at_us: 2,
  history_revision: 3,
  metadata_version: 4,
  message_count: 6,
  active_run_id: null,
};

const snapshot = {
  run_id: RUN_ID,
  session_id: SESSION_ID,
  status: "pending",
  text: "",
  text_truncated: false,
  error: null,
  durable: false,
  terminal: false,
  stop_requested: false,
  reconciliation_required: false,
  revision: null,
  last_event_id: 1,
  accepted_at_us: 10,
  finished_at_us: null,
  tool_calls: [],
};

describe("GatewayClient requests", () => {
  it("lists sessions most recent first and passes the cursor back unchanged", async () => {
    const cursor = { after_updated_at_us: 2, after_id: SESSION_ID };
    const gateway = new FakeGateway().on(
      "GET /api/sessions",
      json({ items: [session], next_cursor: cursor }),
      json({ items: [], next_cursor: null }),
    );
    const first = await gateway.client.listSessions({ limit: 1 });
    expect(first).toEqual({ items: [session], next_cursor: cursor });
    await gateway.client.listSessions({ limit: 1, cursor: first.next_cursor });
    expect(gateway.requests.map((request) => request.path)).toEqual([
      "/api/sessions?order=updated_desc&limit=1",
      `/api/sessions?order=updated_desc&limit=1&after_updated_at_us=2&after_id=${SESSION_ID}`,
    ]);
  });

  it("creates, reads, renames and deletes a session", async () => {
    const gateway = new FakeGateway()
      .on("POST /api/sessions", json(session, 201), json(session, 201))
      .on(`GET /api/sessions/${SESSION_ID}`, json(session))
      .on(
        `PATCH /api/sessions/${SESSION_ID}`,
        json({ ...session, title: "New", metadata_version: 5 }),
      )
      .on(`DELETE /api/sessions/${SESSION_ID}`, new Response(null, { status: 204 }));
    await gateway.client.createSession();
    await gateway.client.createSession("Plans");
    await gateway.client.getSession(SESSION_ID);
    const renamed = await gateway.client.renameSession(SESSION_ID, "New", 4);
    await gateway.client.deleteSession(SESSION_ID);
    expect(renamed.metadata_version).toBe(5);
    expect(gateway.requests.map(({ method, body }) => [method, body])).toEqual([
      ["POST", {}],
      ["POST", { title: "Plans" }],
      ["GET", undefined],
      ["PATCH", { title: "New", expected_version: 4 }],
      ["DELETE", undefined],
    ]);
    expect(gateway.requests[0]?.headers.get("Content-Type")).toBe("application/json");
    expect(gateway.requests[2]?.headers.has("Content-Type")).toBe(false);
  });

  it("reads messages with the paging parameters it was given", async () => {
    const page = {
      items: [
        { seq: 1, message: { role: "user", content: "Hi" } },
        {
          seq: 2,
          message: {
            role: "assistant",
            content: null,
            tool_calls: [
              { id: "c1", type: "function", function: { name: "lookup", arguments: "{" } },
            ],
          },
        },
        { seq: 3, message: { role: "tool", content: "{}", tool_call_id: "c1" } },
        { seq: 4, message: { role: "assistant", content: "Done", refusal: null } },
      ],
      through: 6,
      next_after: 4,
    };
    const gateway = new FakeGateway().on(
      `GET /api/sessions/${SESSION_ID}/messages`,
      json(page),
      json({ items: [], through: 6, next_after: null }),
    );
    const result = await gateway.client.readMessages(SESSION_ID);
    await gateway.client.readMessages(SESSION_ID, { after: 4, limit: 2, through: 6 });
    expect(result.items.map((item) => item.message)).toEqual([
      { role: "user", content: "Hi" },
      {
        role: "assistant",
        content: null,
        tool_calls: [{ id: "c1", name: "lookup", arguments: "{" }],
        refusal: null,
      },
      { role: "tool", content: "{}", tool_call_id: "c1" },
      { role: "assistant", content: "Done", tool_calls: [], refusal: null },
    ]);
    expect(gateway.requests.map((request) => request.path)).toEqual([
      `/api/sessions/${SESSION_ID}/messages`,
      `/api/sessions/${SESSION_ID}/messages?after=4&limit=2&through=6`,
    ]);
  });

  it("starts, reads and stops a run", async () => {
    const gateway = new FakeGateway()
      .on("POST /v1/runs", json(snapshot, 202))
      .on(`GET /v1/runs/${RUN_ID}`, json({ ...snapshot, status: "running" }))
      .on(`POST /v1/runs/${RUN_ID}/stop`, json({ ...snapshot, stop_requested: true }));
    expect(await gateway.client.startRun(SESSION_ID, "Hello")).toEqual(snapshot);
    expect((await gateway.client.getRun(RUN_ID)).status).toBe("running");
    expect((await gateway.client.stopRun(RUN_ID)).stop_requested).toBe(true);
    expect(gateway.requests[0]?.body).toEqual({ session_id: SESSION_ID, message: "Hello" });
  });

  it("accepts a run snapshot from a Gateway without the Web UI fields", async () => {
    const { accepted_at_us: _a, finished_at_us: _f, tool_calls: _t, ...legacy } = snapshot;
    const gateway = new FakeGateway().on(`GET /v1/runs/${RUN_ID}`, json(legacy));
    const result = await gateway.client.getRun(RUN_ID);
    expect(result).toEqual(legacy);
    expect("tool_calls" in result).toBe(false);
  });

  it("reads commit receipts, both saved and not saved", async () => {
    const saved = {
      operation_id: RUN_ID,
      committed: true,
      revision: 3,
      first_seq: 5,
      last_seq: 6,
      status: "completed",
    };
    const gateway = new FakeGateway().on(
      `GET /api/sessions/${SESSION_ID}/commits/${RUN_ID}`,
      json(saved),
      json({ operation_id: RUN_ID, committed: false }),
      errorEnvelope(409, "conflict"),
    );
    expect(await gateway.client.getCommit(SESSION_ID, RUN_ID)).toEqual(saved);
    expect(await gateway.client.getCommit(SESSION_ID, RUN_ID)).toEqual({
      operation_id: RUN_ID,
      committed: false,
    });
    // Not settled yet: the run may still commit.
    await expect(gateway.client.getCommit(SESSION_ID, RUN_ID)).rejects.toMatchObject({
      status: 409,
      code: "conflict",
    });
  });

  it("uses the base URL and escapes path segments", async () => {
    const gateway = new FakeGateway().on("GET /prefix/api/sessions/a%2Fb", json(session));
    const client = new GatewayClient({ baseUrl: "/prefix", fetch: gateway.fetch });
    await client.getSession("a/b");
    expect(gateway.requests[0]?.path).toBe("/prefix/api/sessions/a%2Fb");
  });
});

describe("GatewayClient errors", () => {
  it.each([
    [400, "invalid_request"],
    [403, "forbidden_origin"],
    [404, "not_found"],
    [409, "conflict"],
    [503, "storage_unavailable"],
    [503, "unavailable"],
    [500, "internal_error"],
  ])("turns %i %s into a GatewayError with that code", async (status, code) => {
    const gateway = new FakeGateway().on(
      `GET /api/sessions/${SESSION_ID}`,
      errorEnvelope(status, code),
    );
    const error = await gateway.client.getSession(SESSION_ID).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(GatewayError);
    expect(error).toMatchObject({ status, code, message: `${code} message` });
  });

  it.each([
    ["an HTML error page", new Response("<h1>Bad gateway</h1>", { status: 502 })],
    ["an unknown error code", errorEnvelope(404, "no_such_code")],
    ["an empty body", new Response(null, { status: 404 })],
  ])("reports %s as code unknown rather than guessing", async (_name, response) => {
    const gateway = new FakeGateway().on(`GET /api/sessions/${SESSION_ID}`, response);
    await expect(gateway.client.getSession(SESSION_ID)).rejects.toMatchObject({
      code: "unknown",
      status: response.status,
    });
  });

  it("reports a failed connection as unreachable", async () => {
    const gateway = new FakeGateway().on(
      `GET /api/sessions/${SESSION_ID}`,
      new TypeError("Failed to fetch"),
    );
    const error = await gateway.client.getSession(SESSION_ID).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(GatewayUnreachableError);
    expect((error as Error).cause).toBeInstanceOf(TypeError);
  });

  it("lets an abort through unchanged", async () => {
    const gateway = new FakeGateway();
    const controller = new AbortController();
    controller.abort();
    await expect(gateway.client.getRun(RUN_ID, controller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
  });

  it.each([
    ["a session without an ID", { ...session, id: 1 }],
    ["a count that is not an integer", { ...session, message_count: 1.5 }],
    ["a missing active run field", { ...session, active_run_id: undefined }],
    ["an array", [session]],
  ])("rejects %s", async (_name, body) => {
    const gateway = new FakeGateway().on(`GET /api/sessions/${SESSION_ID}`, json(body));
    await expect(gateway.client.getSession(SESSION_ID)).rejects.toBeInstanceOf(ProtocolError);
  });

  it.each([
    ["an unknown status", { ...snapshot, status: "sleeping" }],
    [
      "a tool call with an unknown status",
      {
        ...snapshot,
        tool_calls: [
          {
            call_id: "c",
            name: "x",
            status: "?",
            duration_s: null,
            is_error: false,
            arguments: null,
            arguments_truncated: false,
          },
        ],
      },
    ],
    [
      "tool arguments that are not an object",
      {
        ...snapshot,
        tool_calls: [
          {
            call_id: "c",
            name: "x",
            status: "ok",
            duration_s: 1,
            is_error: false,
            arguments: [1],
            arguments_truncated: false,
          },
        ],
      },
    ],
  ])("rejects a run snapshot with %s", async (_name, body) => {
    const gateway = new FakeGateway().on(`GET /v1/runs/${RUN_ID}`, json(body));
    await expect(gateway.client.getRun(RUN_ID)).rejects.toBeInstanceOf(ProtocolError);
  });

  it("rejects a message with an unknown role", async () => {
    const gateway = new FakeGateway().on(
      `GET /api/sessions/${SESSION_ID}/messages`,
      json({
        items: [{ seq: 1, message: { role: "robot", content: "x" } }],
        through: 1,
        next_after: null,
      }),
    );
    await expect(gateway.client.readMessages(SESSION_ID)).rejects.toBeInstanceOf(ProtocolError);
  });
});
