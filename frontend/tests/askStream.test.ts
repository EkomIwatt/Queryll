/**
 * The real `fetch` + `ReadableStream` reader — Contract 7 §1, and Contract 9's
 * before-the-stream-opens error path.
 *
 * `fetch` is stubbed here rather than the API client, because this file *is* the fetch layer:
 * the header set, the single refresh-and-retry, and the difference between "the request failed"
 * and "the stream failed" are exactly what is under test.
 */

import { describe, expect, it, vi } from 'vitest';
import { createAskStream, readAskEvents } from '../src/api/askStream';
import { createRawSseAskStream, sseBodyText, splitEvenly } from '../src/api/fakeStream';
import { ApiError, NetworkError } from '../src/api/errors';
import type { AskEvent } from '../src/api/types';
import { askScript, makeCitation } from './harness';

function bodyOf(text: string): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text));
      controller.close();
    },
  });
}

function sseResponse(text: string, init: ResponseInit = {}): Response {
  return new Response(bodyOf(text), {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
    ...init,
  });
}

async function collect(stream: AsyncGenerator<AskEvent>): Promise<AskEvent[]> {
  const events: AskEvent[] = [];
  for await (const event of stream) events.push(event);
  return events;
}

describe('readAskEvents', () => {
  it('yields the contract event sequence from a real body', async () => {
    const events = await collect(readAskEvents(bodyOf(sseBodyText(askScript()))));

    expect(events.map((e) => e.event)).toEqual(['retrieval', 'token', 'token', 'token', 'done']);
  });
});

describe('createRawSseAskStream', () => {
  it('reassembles a `[n]` marker torn across two network chunks', async () => {
    // The server buffers a partial trailing marker before it emits, but the *frame* carrying it
    // can still be split by the network at any byte. This is that case.
    const body = sseBodyText([
      { event: 'retrieval', data: { sources: [makeCitation()], insufficient_context: false } },
      { event: 'token', data: { text: 'Stratified by region [1].' } },
      { event: 'done', data: { message_id: 'm1', citations_used: [1] } },
    ]);
    const cut = body.indexOf('region [1') + 8; // mid-marker
    const stream = createRawSseAskStream([body.slice(0, cut), body.slice(cut)]);

    const events = await collect(stream({ conversationId: 'c1', body: { question: 'q', document_ids: null } }));
    const tokens = events.filter((e) => e.event === 'token');

    expect(tokens).toHaveLength(1);
    expect(tokens[0]).toEqual({ event: 'token', data: { text: 'Stratified by region [1].' } });
  });

  it('produces the same events at every chunk boundary', async () => {
    const body = sseBodyText(askScript());

    for (const count of [2, 4, 7, 25]) {
      const stream = createRawSseAskStream(splitEvenly(body, count));
      const events = await collect(
        stream({ conversationId: 'c1', body: { question: 'q', document_ids: null } }),
      );
      expect(events.map((e) => e.event)).toEqual(['retrieval', 'token', 'token', 'token', 'done']);
    }
  });

  it('propagates a connection that drops mid-body', async () => {
    const body = sseBodyText(askScript());
    const stream = createRawSseAskStream(splitEvenly(body, 4), { errorAfterChunks: 2 });

    await expect(
      collect(stream({ conversationId: 'c1', body: { question: 'q', document_ids: null } })),
    ).rejects.toBeInstanceOf(NetworkError);
  });
});

describe('createAskStream', () => {
  const input = { conversationId: 'conv-1', body: { question: 'q', document_ids: null } };

  it('POSTs with the bearer token, the event-stream Accept header and the JSON body', async () => {
    const fetchImpl = vi.fn(async () => sseResponse(sseBodyText(askScript())));
    const stream = createAskStream({
      baseUrl: 'https://api.test',
      getAccessToken: async () => 'token-1',
      refreshAccessToken: async () => null,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await collect(stream(input));

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.test/api/conversations/conv-1/ask');
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer token-1');
    expect((init.headers as Record<string, string>).Accept).toBe('text/event-stream');
    expect(init.body).toBe(JSON.stringify({ question: 'q', document_ids: null }));
  });

  it('refreshes exactly once on a 401 and retries with the new token', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(new Response('{"error":"expired"}', { status: 401 }))
      .mockResolvedValueOnce(sseResponse(sseBodyText(askScript())));
    const refreshAccessToken = vi.fn(async () => 'token-2');

    const stream = createAskStream({
      baseUrl: 'https://api.test',
      getAccessToken: async () => 'token-1',
      refreshAccessToken,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    const events = await collect(stream(input));

    expect(refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    const retry = fetchImpl.mock.calls[1] as unknown as [string, RequestInit];
    expect((retry[1].headers as Record<string, string>).Authorization).toBe('Bearer token-2');
    expect(events.at(-1)?.event).toBe('done');
  });

  it('gives up after one failed refresh rather than looping', async () => {
    const fetchImpl = vi.fn(async () => new Response('{"error":"nope"}', { status: 401 }));
    const refreshAccessToken = vi.fn(async () => null);

    const stream = createAskStream({
      baseUrl: 'https://api.test',
      getAccessToken: async () => 'token-1',
      refreshAccessToken,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(collect(stream(input))).rejects.toMatchObject({ status: 401 });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(refreshAccessToken).toHaveBeenCalledTimes(1);
  });

  it('surfaces the Contract 9 envelope for an error before the stream opens', async () => {
    // Anthropic failed before the first frame: 502 with a JSON envelope, not an `error` event.
    const fetchImpl = vi.fn(
      async () =>
        new Response('{"error":"The answer service is unavailable right now."}', { status: 502 }),
    );

    const stream = createAskStream({
      baseUrl: 'https://api.test',
      getAccessToken: async () => 'token-1',
      refreshAccessToken: async () => null,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(collect(stream(input))).rejects.toThrow(
      'The answer service is unavailable right now.',
    );
    await expect(collect(stream(input))).rejects.toBeInstanceOf(ApiError);
  });

  it('reports a transport failure as a NetworkError, not as a contract error', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    });

    const stream = createAskStream({
      baseUrl: 'https://api.test',
      getAccessToken: async () => null,
      refreshAccessToken: async () => null,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(collect(stream(input))).rejects.toBeInstanceOf(NetworkError);
  });
});
