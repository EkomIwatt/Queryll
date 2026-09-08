/**
 * The fake stream reader.
 *
 * Built before any chat UI, on purpose: every interesting thing about the answer experience —
 * sources landing before the first token, token-by-token rendering, a mid-stream error after
 * partial text, `insufficient_context`, a `[n]` marker split across two network chunks,
 * a body that dies without a terminal frame — is a *timing and framing* property. None of it is
 * reachable through a mocked promise, and all of it is deterministic through here.
 *
 * Three fakes, all satisfying `AskStreamFn`, in increasing order of realism:
 *
 *   `createScriptedAskStream`  — yields decoded events on a timer. Fastest to write a test with.
 *   `createManualAskStream`    — the test emits each event by hand. Total control over interleaving.
 *   `createRawSseAskStream`    — feeds raw SSE *text*, in the exact chunk splits you specify,
 *                                through the real `SseDecoder`. This is the only one that can
 *                                prove the parser survives a frame torn in half.
 *
 * This module is shipped in `src/`, not `tests/`, so the dev-mode mock backend can drive the
 * real chat UI with it and a human can click through the whole experience with no API running.
 */

import { NetworkError } from './errors';
import { readAskEvents, type AskStreamFn, type AskStreamInput } from './askStream';
import type { AskEvent } from './types';

/* ------------------------------------------------------------------ *
 * Wire-format helpers
 * ------------------------------------------------------------------ */

/** Serialise one event to the exact bytes Contract 7 §2 puts on the wire. */
export function sseFrameText(event: AskEvent): string {
  return `event: ${event.event}\ndata: ${JSON.stringify(event.data)}\n\n`;
}

/** The comment heartbeat the server sends every 15s while the model is thinking. */
export const SSE_HEARTBEAT = ': ping\n\n';

/** Serialise a whole script to a single SSE body. */
export function sseBodyText(events: readonly AskEvent[]): string {
  return events.map(sseFrameText).join('');
}

/* ------------------------------------------------------------------ *
 * 1. Scripted — decoded events on a timer
 * ------------------------------------------------------------------ */

export interface ScriptedStep {
  event: AskEvent;
  /** Delay *before* this event is yielded. Defaults to `defaultDelayMs`. */
  delayMs?: number;
}

export interface ScriptedOptions {
  /** Delay before every step that does not specify its own. Default 0 — tests stay fast. */
  defaultDelayMs?: number;
  /** Thrown after the last step, to simulate a body that dies with no terminal frame. */
  throwAtEnd?: Error;
  /** Observe what the chat layer actually sent. */
  onRequest?: (input: AskStreamInput) => void;
}

export function createScriptedAskStream(
  script: readonly (AskEvent | ScriptedStep)[],
  options: ScriptedOptions = {},
): AskStreamFn {
  const steps: ScriptedStep[] = script.map((s) => (isAskEvent(s) ? { event: s } : s));

  return async function* scriptedStream(input) {
    options.onRequest?.(input);
    for (const step of steps) {
      const delay = step.delayMs ?? options.defaultDelayMs ?? 0;
      if (delay > 0) await sleep(delay, input.signal);
      throwIfAborted(input.signal);
      yield step.event;
    }
    if (options.throwAtEnd) throw options.throwAtEnd;
  };
}

function isAskEvent(value: AskEvent | ScriptedStep): value is AskEvent {
  return typeof (value as AskEvent).event === 'string';
}

/* ------------------------------------------------------------------ *
 * 2. Manual — the test drives every emission
 * ------------------------------------------------------------------ */

type ManualItem =
  | { kind: 'event'; event: AskEvent; taken: () => void }
  | { kind: 'close' }
  | { kind: 'fail'; error: Error };

export interface ManualAskStream {
  /** Hand this to the component under test. */
  stream: AskStreamFn;
  /** Push one event to the open stream. Resolves once the consumer has taken it. */
  emit(event: AskEvent): Promise<void>;
  /** End the stream cleanly (as a body that closes). */
  close(): void;
  /** End the stream by throwing — a dropped connection, not an `error` frame. */
  fail(error?: Error): void;
  /** The input the consumer opened the stream with, once it has opened one. */
  readonly lastInput: AskStreamInput | null;
  /** Resolves when the consumer has opened the stream. */
  opened(): Promise<void>;
}

export function createManualAskStream(): ManualAskStream {
  const queue: ManualItem[] = [];
  let wake: (() => void) | null = null;
  let lastInput: AskStreamInput | null = null;
  let signalOpened: (() => void) | null = null;
  let openedFlag = false;

  const nudge = (): void => {
    const w = wake;
    wake = null;
    w?.();
  };

  const stream: AskStreamFn = async function* manualStream(input) {
    lastInput = input;
    openedFlag = true;
    signalOpened?.();

    // Abort has to wake the parked generator, or a cancelled stream would sit here forever —
    // the fake equivalent of `fetch` tearing down the socket.
    const onAbort = () => nudge();
    input.signal?.addEventListener('abort', onAbort, { once: true });

    try {
      for (;;) {
        throwIfAborted(input.signal);
        const next = queue.shift();
        if (!next) {
          await new Promise<void>((resolve) => {
            wake = resolve;
          });
          continue;
        }
        if (next.kind === 'close') return;
        if (next.kind === 'fail') throw next.error;
        yield next.event;
        next.taken();
      }
    } finally {
      input.signal?.removeEventListener('abort', onAbort);
    }
  };

  return {
    stream,
    emit(event) {
      return new Promise<void>((resolve) => {
        queue.push({ kind: 'event', event, taken: resolve });
        nudge();
      });
    },
    close() {
      queue.push({ kind: 'close' });
      nudge();
    },
    fail(error = new NetworkError()) {
      queue.push({ kind: 'fail', error });
      nudge();
    },
    get lastInput() {
      return lastInput;
    },
    opened() {
      if (openedFlag) return Promise.resolve();
      return new Promise<void>((resolve) => {
        signalOpened = resolve;
      });
    },
  };
}

/* ------------------------------------------------------------------ *
 * 3. Raw SSE — real bytes, real parser, your chunk boundaries
 * ------------------------------------------------------------------ */

export interface RawSseOptions {
  /** Delay before each chunk is enqueued. Default 0. */
  chunkDelayMs?: number;
  /** Error the body after this many chunks, to simulate the connection dropping. */
  errorAfterChunks?: number;
}

/**
 * Feed raw SSE text through the real decoder, split at exactly the offsets given.
 *
 * Pass one string per network chunk. Splitting a frame — or a `[n]` marker inside a token —
 * across two entries is the whole point: that is the failure the contract warns about and it is
 * not reproducible any other way.
 */
export function createRawSseAskStream(
  chunks: readonly string[],
  options: RawSseOptions = {},
): AskStreamFn {
  return async function* rawStream(input) {
    const body = readableStreamFromChunks(chunks, options, input.signal);
    yield* readAskEvents(body);
  };
}

/** Split an SSE body into `count` roughly equal chunks — a cheap way to fuzz frame boundaries. */
export function splitEvenly(text: string, count: number): string[] {
  if (count <= 1) return [text];
  const size = Math.ceil(text.length / count);
  const out: string[] = [];
  for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size));
  return out;
}

function readableStreamFromChunks(
  chunks: readonly string[],
  options: RawSseOptions,
  signal?: AbortSignal,
): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let index = 0;

  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (options.errorAfterChunks !== undefined && index === options.errorAfterChunks) {
        controller.error(new NetworkError());
        return;
      }
      if (index >= chunks.length) {
        controller.close();
        return;
      }
      const delay = options.chunkDelayMs ?? 0;
      if (delay > 0) await sleep(delay, signal);
      controller.enqueue(encoder.encode(chunks[index] ?? ''));
      index += 1;
    },
  });
}

/* ------------------------------------------------------------------ *
 * Shared
 * ------------------------------------------------------------------ */

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(abortError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw abortError();
}

function abortError(): DOMException {
  return new DOMException('The answer was cancelled.', 'AbortError');
}
