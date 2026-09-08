/**
 * Contract 9 — the error envelope is `{ "error": "<sentence>" }` for every non-2xx
 * response, from every endpoint. That sentence is safe to render directly, so `ApiError.message`
 * is always user-facing copy and the UI never invents its own wording on top of it.
 */

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }

  /** Contract 9: another user's resource, or a dangling citation, is 404 — never 403. */
  get isNotFound(): boolean {
    return this.status === 404;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** Contract 6 §5/§6 — delete or re-index while `processing`. */
  get isConflict(): boolean {
    return this.status === 409;
  }
}

/**
 * The network failed, or the response body could not be read. There is no server sentence to
 * show, so this carries our own — the only place the UI writes error copy itself.
 */
export class NetworkError extends Error {
  constructor(message = 'Could not reach the server. Check your connection and try again.') {
    super(message);
    this.name = 'NetworkError';
  }
}

/** The SSE stream produced something that is not a legal Contract 7 §3 frame. */
export class StreamProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'StreamProtocolError';
  }
}

/** A sentence safe to render for any thrown value, preferring the server's own wording. */
export function messageFor(error: unknown): string {
  if (error instanceof ApiError || error instanceof NetworkError) return error.message;
  if (error instanceof StreamProtocolError) {
    return 'The answer stream ended unexpectedly. Please ask again.';
  }
  return 'Something went wrong. Please try again.';
}
