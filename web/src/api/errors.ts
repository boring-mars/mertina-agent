/** Errors raised by the Gateway client, each telling the UI what kind of failure it is. */

import { isRecord } from "./decode";

/** Codes the Gateway puts in its JSON error envelope. */
export const GATEWAY_ERROR_CODES = [
  "invalid_request",
  "forbidden_origin",
  "not_found",
  "method_not_allowed",
  "request_timeout",
  "request_too_large",
  "unsupported_media_type",
  "conflict",
  "resync_required",
  "unavailable",
  "storage_unavailable",
  "internal_error",
] as const;
export type GatewayErrorCode = (typeof GATEWAY_ERROR_CODES)[number];

/** The Gateway answered with an error status. */
export class GatewayError extends Error {
  override readonly name = "GatewayError";

  constructor(
    readonly status: number,
    /** A known code, `"unknown"` for a response without a recognisable envelope. */
    readonly code: GatewayErrorCode | "unknown",
    message: string,
  ) {
    super(message);
  }
}

/** The Gateway could not be reached at all: it is not running, or the network failed. */
export class GatewayUnreachableError extends Error {
  override readonly name = "GatewayUnreachableError";
}

function isGatewayErrorCode(value: unknown): value is GatewayErrorCode {
  return (GATEWAY_ERROR_CODES as readonly unknown[]).includes(value);
}

/**
 * Build a {@link GatewayError} from an error response.
 *
 * A body that is not the Gateway's envelope (for example a proxy's HTML error page, or a
 * route an older Gateway does not have) becomes code `"unknown"` rather than being
 * mistaken for a Gateway decision.
 */
export async function gatewayError(response: Response): Promise<GatewayError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Not JSON: fall through to the generic error below.
  }
  if (isRecord(body) && isRecord(body.error)) {
    const { code, message } = body.error;
    if (isGatewayErrorCode(code)) {
      return new GatewayError(response.status, code, typeof message === "string" ? message : code);
    }
  }
  return new GatewayError(response.status, "unknown", `HTTP ${response.status}`);
}
