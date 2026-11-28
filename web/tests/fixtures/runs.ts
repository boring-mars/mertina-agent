/** Load the recorded run fixtures in `runs/`, decoded the same way the app decodes them. */

import type { EventEnvelope } from "@/api/sse";
import { decodeRunSnapshot, type RunSnapshot } from "@/api/types";
import { decodeRunEvent, type RunEvent } from "@/runs/events";

interface RawFixture {
  readonly description: string;
  readonly message: string;
  readonly start: unknown;
  readonly events: readonly { readonly at: number; readonly envelope: EventEnvelope }[];
  readonly final: unknown;
}

export interface RunFixture {
  readonly name: string;
  readonly description: string;
  readonly start: RunSnapshot;
  readonly events: readonly { readonly at: number; readonly event: RunEvent }[];
  readonly envelopes: readonly EventEnvelope[];
  readonly final: RunSnapshot;
}

const files = import.meta.glob<RawFixture>("./runs/*.json", { eager: true, import: "default" });

export const runFixtures: ReadonlyMap<string, RunFixture> = new Map(
  Object.entries(files).map(([path, raw]) => {
    const name = path.replace(/^.*\/(.+)\.json$/, "$1");
    return [
      name,
      {
        name,
        description: raw.description,
        start: decodeRunSnapshot(raw.start),
        events: raw.events.map(({ at, envelope }) => ({ at, event: decodeRunEvent(envelope) })),
        envelopes: raw.events.map(({ envelope }) => envelope),
        final: decodeRunSnapshot(raw.final),
      },
    ];
  }),
);

export function runFixture(name: string): RunFixture {
  const fixture = runFixtures.get(name);
  if (fixture === undefined) {
    throw new Error(`No run fixture named ${name}`);
  }
  return fixture;
}
