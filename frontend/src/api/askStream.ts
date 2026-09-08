/**
 * Contract 7 §1 — the answer transport.
 *
 * `POST /api/conversations/{id}/ask` read via `fetch` + `ReadableStream`, deliberately not
 * `EventSource`: `EventSource` can neither POST nor carry an `Authorization` header.
 *
 * Everything downstream of here — the chat UI, its tests, the fake stream reader — talks to
 * `AskStreamFn` and nothing else, so the real network path and the scripted one are
 * interchangeable by construction.
 */

import { ApiError, NetworkError } from './errors';
import { SseDecoder, decodeAskEvent } from './sse';
import type { AskEvent, AskRequest } from './types';
import { isErrorEnvelope } from './types';

export interface AskStreamInput {
  conversationId: string;
  body: AskRequest;
  signal?: AbortSignal;
}

/** The one interface the chat layer knows. Implemented by the real reader and by the fakes. */
export type AskStreamFn = (input: AskStreamInput) => AsyncGenerator<AskEvent, void, undefined>;

/**
 * Turn a response body into Contract 7 §3 events.
 *
 * Shared by the real network reader and by the raw-SSE fake, so the fake exercises this exact
 * parser rather than a second implementation of it.
 */
export async function* readAskEvents(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<AskEvent, void, undefined> {
  const reader = body.getReader();
  const decoder = new SseDecoder();
  const utf8 = new TextDecoder();

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      // `stream: true` keeps a multi-byte character split across chunks intact.
      for (const frame of decoder.push(utf8.decode(value, { stream: true }))) {
        const event = decodeAskEvent(frame);
        if (event) yield event;
      }
    }
    for (const frame of decoder.flush()) {
      const event = decodeAskEvent(frame);
      if (event) yield event;
    }
  } finally {
    // Cancelling the reader is what actually closes the socket when the consumer stops early.
    // Contract 7 §8: the server persists the message anyway, so this loses nothing.
    reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export interface AskStreamDeps {
  baseUrl: string;
  /** Resolves the current access token, refreshing it first if it is missing or stale. */
  getAccessToken: () => Promise<string | null>;
  /** Called once on a 401; returns a fresh token, or null if the session is truly gone. */
  refreshAccessToken: () => Promise<string | null>;
  fetchImpl?: typeof fetch;
}

/** Build the real, network-backed `AskStreamFn`. */
export function createAskStream(deps: AskStreamDeps): AskStreamFn {
  const doFetch = deps.fetchImpl ?? globalThis.fetch.bind(globalThis);

  return async function* askStream(input) {
    const url = `${deps.baseUrl}/api/conversations/${encodeURIComponent(input.conversationId)}/ask`;

    const send = async (token: string | null): Promise<Response> => {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      };
      if (token) headers.Authorization = `Bearer ${token}`;
      try {
        return await doFetch(url, {
          method: 'POST',
          headers,
          body: JSON.stringify(input.body),
          credentials: 'include',
          ...(input.signal ? { signal: input.signal } : {}),
        });
      } catch (cause) {
        if (input.signal?.aborted) throw cause;
        throw new NetworkError();
      }
    };

    let response = await send(await deps.getAccessToken());

    // Contract 9: refresh once on 401, then give up and let the caller route to login.
    if (response.status === 401) {
      const refreshed = await deps.refreshAccessToken();
      if (refreshed) response = await send(refreshed);
    }

    if (!response.ok) {
      throw new ApiError(await errorSentence(response), response.status);
    }
    if (!response.body) {
      throw new NetworkError('The answer stream could not be opened.');
    }

    yield* readAskEvents(response.body);
  };
}

/** Contract 9 — errors *before* the stream opens use the JSON envelope, not an `error` frame. */
async function errorSentence(response: Response): Promise<string> {
  try {
    const payload: unknown = await response.json();
    if (isErrorEnvelope(payload)) return payload.error;
  } catch {
    // fall through to a generic sentence
  }
  return response.status === 401
    ? 'Your session has expired. Please sign in again.'
    : 'The answer could not be started. Please try again.';
}
