/**
 * The SSE decoder — Contract 7 §2.
 *
 * These are the framing failures that only ever show up against a real network: a frame torn in
 * half between two TCP chunks, a heartbeat arriving mid-answer, CRLF line endings from a proxy
 * that rewrote them. None are reachable through a mocked promise, all are trivial here.
 */

import { describe, expect, it } from 'vitest';
import { SseDecoder, decodeAskEvent } from '../src/api/sse';
import { StreamProtocolError } from '../src/api/errors';
import { SSE_HEARTBEAT, sseBodyText, splitEvenly } from '../src/api/fakeStream';
import { askScript, makeCitation } from './harness';

describe('SseDecoder', () => {
  it('decodes a complete frame', () => {
    const decoder = new SseDecoder();
    const frames = decoder.push('event: token\ndata: {"text":"hello"}\n\n');

    expect(frames).toEqual([{ event: 'token', data: '{"text":"hello"}' }]);
  });

  it('holds a frame that arrives split across two chunks until it is complete', () => {
    const decoder = new SseDecoder();

    expect(decoder.push('event: tok')).toEqual([]);
    expect(decoder.push('en\ndata: {"text":"hel')).toEqual([]);
    expect(decoder.push('lo"}\n\n')).toEqual([
      { event: 'token', data: '{"text":"hello"}' },
    ]);
  });

  it('emits several frames arriving in one chunk, in order', () => {
    const decoder = new SseDecoder();
    const frames = decoder.push(
      'event: token\ndata: {"text":"a"}\n\nevent: token\ndata: {"text":"b"}\n\n',
    );

    expect(frames.map((f) => f.data)).toEqual(['{"text":"a"}', '{"text":"b"}']);
  });

  it('swallows the `: ping` heartbeat without emitting an event', () => {
    const decoder = new SseDecoder();

    expect(decoder.push(SSE_HEARTBEAT)).toEqual([]);
    expect(decoder.push('event: token\ndata: {"text":"a"}\n\n')).toHaveLength(1);
  });

  it('normalises CRLF line endings', () => {
    const decoder = new SseDecoder();
    const frames = decoder.push('event: done\r\ndata: {"message_id":"m1"}\r\n\r\n');

    expect(frames).toEqual([{ event: 'done', data: '{"message_id":"m1"}' }]);
  });

  it('joins multi-line data with newlines, per the SSE spec', () => {
    const decoder = new SseDecoder();
    const frames = decoder.push('event: token\ndata: one\ndata: two\n\n');

    expect(frames[0]?.data).toBe('one\ntwo');
  });

  it('flushes a trailing frame that the body ended without terminating', () => {
    const decoder = new SseDecoder();

    expect(decoder.push('event: done\ndata: {"message_id":"m1"}')).toEqual([]);
    expect(decoder.flush()).toEqual([{ event: 'done', data: '{"message_id":"m1"}' }]);
  });

  it('survives an entire answer body split at every possible chunk size', () => {
    const body = sseBodyText(askScript({ sources: [makeCitation()] }));

    for (const chunkCount of [1, 2, 3, 5, 8, 13, 40, body.length]) {
      const decoder = new SseDecoder();
      const events = splitEvenly(body, chunkCount)
        .flatMap((chunk) => decoder.push(chunk))
        .concat(decoder.flush())
        .map((frame) => decodeAskEvent(frame));

      expect(events.map((e) => e?.event)).toEqual([
        'retrieval',
        'token',
        'token',
        'token',
        'done',
      ]);
    }
  });
});

describe('decodeAskEvent', () => {
  it('decodes each of the four contract events', () => {
    expect(
      decodeAskEvent({ event: 'retrieval', data: '{"sources":[],"insufficient_context":true}' }),
    ).toEqual({ event: 'retrieval', data: { sources: [], insufficient_context: true } });

    expect(decodeAskEvent({ event: 'token', data: '{"text":"hi"}' })).toEqual({
      event: 'token',
      data: { text: 'hi' },
    });

    expect(
      decodeAskEvent({ event: 'done', data: '{"message_id":"m1","citations_used":[1,3]}' }),
    ).toEqual({ event: 'done', data: { message_id: 'm1', citations_used: [1, 3] } });

    expect(
      decodeAskEvent({ event: 'error', data: '{"error":"Upstream failed.","code":"upstream"}' }),
    ).toEqual({ event: 'error', data: { error: 'Upstream failed.', code: 'upstream' } });
  });

  it('ignores an event name the contract does not define, rather than crashing', () => {
    expect(decodeAskEvent({ event: 'progress', data: '{"pct":10}' })).toBeNull();
    expect(decodeAskEvent({ event: 'message', data: 'hello' })).toBeNull();
  });

  it('throws on a known event carrying malformed JSON', () => {
    expect(() => decodeAskEvent({ event: 'token', data: '{"text":' })).toThrow(
      StreamProtocolError,
    );
  });

  it('throws when a token frame has no text', () => {
    expect(() => decodeAskEvent({ event: 'token', data: '{"nope":1}' })).toThrow(
      StreamProtocolError,
    );
  });

  it('throws when a done frame has no message id, so the UI cannot hang waiting', () => {
    expect(() => decodeAskEvent({ event: 'done', data: '{}' })).toThrow(StreamProtocolError);
  });

  it('defaults a malformed error frame to a renderable sentence', () => {
    const decoded = decodeAskEvent({ event: 'error', data: '{}' });

    expect(decoded).toEqual({
      event: 'error',
      data: { error: 'The answer could not be completed.', code: 'unknown' },
    });
  });
});
