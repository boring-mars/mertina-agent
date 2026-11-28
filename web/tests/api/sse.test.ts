import { describe, expect, it } from "vitest";

import { ProtocolError } from "@/api/decode";
import { GatewayError } from "@/api/errors";
import { readRunEvents, SseParser, type SseFrame, type StreamItem } from "@/api/sse";

import { errorEnvelope, eventStream, FakeGateway, frames } from "../gateway";

const RUN = "r".repeat(32);

function parseAll(chunks: readonly string[]): SseFrame[] {
  const parser = new SseParser();
  return chunks.flatMap((chunk) => parser.push(chunk));
}

async function collect(stream: AsyncIterable<StreamItem>): Promise<StreamItem[]> {
  const items: StreamItem[] = [];
  for await (const item of stream) {
    items.push(item);
  }
  return items;
}

describe("SseParser", () => {
  const stream =
    ": keepalive\n\n" +
    'id: 1\nevent: run.accepted\ndata: {"a":1}\n\n' +
    "id: 2\r\nevent: text.delta\r\ndata: first\r\ndata: second\r\n\r\n" +
    "event: resync_required\rdata: {}\r\r";
  const expected: SseFrame[] = [
    { id: "1", event: "run.accepted", data: '{"a":1}' },
    { id: "2", event: "text.delta", data: "first\nsecond" },
    // The ID carries over until the stream sets a new one, as in the SSE spec.
    { id: "2", event: "resync_required", data: "{}" },
  ];

  it("parses LF, CRLF and CR line endings, multi-line data and comments", () => {
    expect(parseAll([stream])).toEqual(expected);
  });

  it("gives the same frames however the stream is split", () => {
    for (let cut = 0; cut <= stream.length; cut += 1) {
      expect(parseAll([stream.slice(0, cut), stream.slice(cut)])).toEqual(expected);
    }
    expect(
      parseAll(Array.from({ length: stream.length }, (_, index) => stream.charAt(index))),
    ).toEqual(expected);
  });

  it("follows the field rules: no space after the colon, bare fields, unknown fields", () => {
    expect(parseAll(["event:x\ndata\nretry: 5\nid: 3\0\n\n"])).toEqual([
      { id: null, event: "x", data: "" },
    ]);
  });

  it("dispatches nothing for an event with no data, and defaults the event name", () => {
    expect(parseAll(["event: lonely\n\ndata: hi\n\n"])).toEqual([
      { id: null, event: "message", data: "hi" },
    ]);
  });

  it("keeps an unfinished frame until its blank line arrives", () => {
    const parser = new SseParser();
    expect(parser.push("id: 1\nevent: run.started\ndata: {}\n")).toEqual([]);
    expect(parser.push("")).toEqual([]);
    expect(parser.push("\n")).toEqual([{ id: "1", event: "run.started", data: "{}" }]);
  });
});

describe("readRunEvents", () => {
  const accepted = { id: 1, run_id: RUN, type: "run.accepted", data: { status: "pending" } };
  const started = { id: 2, run_id: RUN, type: "run.started", data: { status: "running" } };

  it("sends the cursor and yields decoded envelopes, skipping keepalives", async () => {
    const gateway = new FakeGateway().on(
      `GET /v1/runs/${RUN}/events`,
      eventStream([
        frames([accepted]).slice(0, 20),
        frames([accepted]).slice(20),
        ": keepalive\n\n",
        frames([started]),
      ]),
    );
    const items = await collect(readRunEvents(gateway.client, RUN, 7));
    expect(items).toEqual([
      { kind: "event", envelope: accepted },
      { kind: "event", envelope: started },
    ]);
    const [request] = gateway.requests;
    expect(request?.headers.get("Last-Event-ID")).toBe("7");
    expect(request?.headers.get("Accept")).toBe("text/event-stream");
  });

  it("reports an in-stream resync and stops reading", async () => {
    const gateway = new FakeGateway().on(
      `GET /v1/runs/${RUN}/events`,
      eventStream([
        frames([accepted]),
        'event: resync_required\ndata: {"code":"resync_required"}\n\n',
        frames([started]),
      ]),
    );
    expect(await collect(readRunEvents(gateway.client, RUN, 0))).toEqual([
      { kind: "event", envelope: accepted },
      { kind: "resync" },
    ]);
  });

  it("throws the Gateway's 409 for a cursor that is no longer buffered", async () => {
    const gateway = new FakeGateway().on(
      `GET /v1/runs/${RUN}/events`,
      errorEnvelope(409, "resync_required"),
    );
    const error = await collect(readRunEvents(gateway.client, RUN, 3)).catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(GatewayError);
    expect(error).toMatchObject({ status: 409, code: "resync_required" });
  });

  it.each([
    ["data that is not JSON", "id: 1\nevent: run.accepted\ndata: nope\n\n"],
    [
      "an envelope without data",
      `id: 1\nevent: run.accepted\ndata: ${JSON.stringify({ id: 1, run_id: RUN, type: "run.accepted" })}\n\n`,
    ],
    [
      "a frame ID that differs from the envelope",
      `id: 9\nevent: run.accepted\ndata: ${JSON.stringify(accepted)}\n\n`,
    ],
    [
      "a frame event that differs from the envelope",
      `id: 1\nevent: run.started\ndata: ${JSON.stringify(accepted)}\n\n`,
    ],
  ])("rejects %s", async (_name, body) => {
    const gateway = new FakeGateway().on(`GET /v1/runs/${RUN}/events`, eventStream([body]));
    await expect(collect(readRunEvents(gateway.client, RUN, 0))).rejects.toBeInstanceOf(
      ProtocolError,
    );
  });

  it("passes a broken connection through to the caller", async () => {
    const cut = new TypeError("network error");
    const gateway = new FakeGateway().on(
      `GET /v1/runs/${RUN}/events`,
      eventStream([frames([accepted])], { fail: cut }),
    );
    const seen: StreamItem[] = [];
    const error = await (async () => {
      for await (const item of readRunEvents(gateway.client, RUN, 0)) {
        seen.push(item);
      }
    })().catch((caught: unknown) => caught);
    expect(seen).toHaveLength(1);
    expect(error).toBe(cut);
  });
});
