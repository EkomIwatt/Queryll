/**
 * Incremental SSE frame decoding — Contract 7 §2.
 *
 * Frames are `event: <name>\n data: <json>\n\n`. A comment heartbeat (`: ping\n\n`) is sent
 * every 15 seconds while the model is thinking and carries no event, so it must be swallowed
 * rather than surfaced.
 *
 * The decoder is deliberately a pure, stateful object rather than a stream transform: a frame
 * routinely straddles two network chunks, and the only way to be sure that case is handled is
 * to be able to feed it arbitrary splits in a test.
 */

import { StreamProtocolError } from './errors';
import type {
  AskEvent,
  Citation,
  DoneEventData,
  RetrievalEventData,
  StreamErrorEventData,
  TokenEventData,
} from './types';

export interface SseFrame {
  /** Defaults to `message` per the SSE spec when the frame carries no `event:` line. */
  event: string;
  /** `data:` lines joined with `\n`, per the SSE spec. */
  data: string;
}

export class SseDecoder {
  private buffer = '';

  /** Feed a decoded text chunk; returns every frame that became complete. */
  push(text: string): SseFrame[] {
    this.buffer += text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    const frames: SseFrame[] = [];

    let boundary = this.buffer.indexOf('\n\n');
    while (boundary !== -1) {
      const block = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const frame = parseBlock(block);
      if (frame) frames.push(frame);
      boundary = this.buffer.indexOf('\n\n');
    }

    return frames;
  }

  /** Flush a trailing frame that was not terminated by a blank line before the body ended. */
  flush(): SseFrame[] {
    const block = this.buffer;
    this.buffer = '';
    const frame = block.trim() ? parseBlock(block) : null;
    return frame ? [frame] : [];
  }
}

function parseBlock(block: string): SseFrame | null {
  let event = '';
  const dataLines: string[] = [];

  for (const line of block.split('\n')) {
    if (line === '' || line.startsWith(':')) continue; // blank line or heartbeat comment
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    if (field === 'event') event = value;
    else if (field === 'data') dataLines.push(value);
    // `id` and `retry` are unused by Contract 7 and are ignored.
  }

  if (dataLines.length === 0 && event === '') return null;
  return { event: event || 'message', data: dataLines.join('\n') };
}

/* ------------------------------------------------------------------ *
 * Frame -> AskEvent, with validation
 * ------------------------------------------------------------------ */

/**
 * Decode one frame into a Contract 7 §3 event.
 *
 * Returns `null` for an event name the contract does not define, so a future server-side
 * addition degrades to "ignored" rather than "crashed". A *known* event whose payload is
 * malformed throws, because silently dropping a `done` would hang the UI forever.
 */
export function decodeAskEvent(frame: SseFrame): AskEvent | null {
  if (!isKnownEvent(frame.event)) return null;

  let payload: unknown;
  try {
    payload = JSON.parse(frame.data) as unknown;
  } catch {
    throw new StreamProtocolError(`\`${frame.event}\` frame carried data that is not JSON`);
  }

  switch (frame.event) {
    case 'retrieval':
      return { event: 'retrieval', data: asRetrieval(payload) };
    case 'token':
      return { event: 'token', data: asToken(payload) };
    case 'done':
      return { event: 'done', data: asDone(payload) };
    case 'error':
      return { event: 'error', data: asStreamError(payload) };
  }
}

const KNOWN_EVENTS = ['retrieval', 'token', 'done', 'error'] as const;
type KnownEvent = (typeof KNOWN_EVENTS)[number];

function isKnownEvent(name: string): name is KnownEvent {
  return (KNOWN_EVENTS as readonly string[]).includes(name);
}

function record(value: unknown, event: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new StreamProtocolError(`\`${event}\` frame did not carry a JSON object`);
  }
  return value as Record<string, unknown>;
}

function asRetrieval(value: unknown): RetrievalEventData {
  const obj = record(value, 'retrieval');
  const sources = Array.isArray(obj.sources) ? (obj.sources as Citation[]) : [];
  return { sources, insufficient_context: obj.insufficient_context === true };
}

function asToken(value: unknown): TokenEventData {
  const obj = record(value, 'token');
  if (typeof obj.text !== 'string') {
    throw new StreamProtocolError('`token` frame had no `text` string');
  }
  return { text: obj.text };
}

function asDone(value: unknown): DoneEventData {
  const obj = record(value, 'done');
  if (typeof obj.message_id !== 'string') {
    throw new StreamProtocolError('`done` frame had no `message_id`');
  }
  const used = Array.isArray(obj.citations_used)
    ? obj.citations_used.filter((n): n is number => typeof n === 'number')
    : [];
  return { message_id: obj.message_id, citations_used: used };
}

function asStreamError(value: unknown): StreamErrorEventData {
  const obj = record(value, 'error');
  return {
    error:
      typeof obj.error === 'string' && obj.error
        ? obj.error
        : 'The answer could not be completed.',
    code: typeof obj.code === 'string' ? obj.code : 'unknown',
  };
}
