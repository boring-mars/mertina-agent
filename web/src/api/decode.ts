/**
 * Small runtime decoders for Gateway JSON.
 *
 * Every response is checked before the rest of the app sees it, so components and the
 * run reducer can trust the types in `types.ts`. Unknown extra fields are ignored, which
 * keeps an older UI working against a newer Gateway that adds fields.
 */

export type JsonRecord = Readonly<Record<string, unknown>>;

/** A Gateway response did not have the shape this UI expects. */
export class ProtocolError extends Error {
  override readonly name = "ProtocolError";
}

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function record(value: unknown, where: string): JsonRecord {
  if (!isRecord(value)) {
    throw new ProtocolError(`${where} must be an object`);
  }
  return value;
}

export function array(value: unknown, where: string): readonly unknown[] {
  if (!Array.isArray(value)) {
    throw new ProtocolError(`${where} must be an array`);
  }
  return value;
}

export function string(source: JsonRecord, key: string, where: string): string {
  const value = source[key];
  if (typeof value !== "string") {
    throw new ProtocolError(`${where}.${key} must be a string`);
  }
  return value;
}

export function nullableString(source: JsonRecord, key: string, where: string): string | null {
  return source[key] === null ? null : string(source, key, where);
}

export function integer(source: JsonRecord, key: string, where: string): number {
  const value = source[key];
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    throw new ProtocolError(`${where}.${key} must be an integer`);
  }
  return value;
}

export function nullableInteger(source: JsonRecord, key: string, where: string): number | null {
  return source[key] === null ? null : integer(source, key, where);
}

/** Read a field that an older Gateway may omit; `undefined` means "not sent". */
export function optional<T>(
  source: JsonRecord,
  key: string,
  read: (source: JsonRecord, key: string, where: string) => T,
  where: string,
): T | undefined {
  return source[key] === undefined ? undefined : read(source, key, where);
}

export function number(source: JsonRecord, key: string, where: string): number {
  const value = source[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ProtocolError(`${where}.${key} must be a finite number`);
  }
  return value;
}

export function nullableNumber(source: JsonRecord, key: string, where: string): number | null {
  return source[key] === null ? null : number(source, key, where);
}

export function boolean(source: JsonRecord, key: string, where: string): boolean {
  const value = source[key];
  if (typeof value !== "boolean") {
    throw new ProtocolError(`${where}.${key} must be a boolean`);
  }
  return value;
}

export function oneOf<T extends string>(
  source: JsonRecord,
  key: string,
  allowed: readonly T[],
  where: string,
): T {
  const value = string(source, key, where);
  if (!(allowed as readonly string[]).includes(value)) {
    throw new ProtocolError(`${where}.${key} has an unknown value`);
  }
  return value as T;
}
