/**
 * The answer stream, as a state machine — Contract 7 §3.
 *
 * The event order is contractual and strict: `retrieval` exactly once and always first, then zero
 * or more `token`s in order, then exactly one terminal `done` — or an `error` frame in its place.
 * This hook is the only thing in the app that knows that sequence, and everything it exposes is
 * derived from it.
 *
 * Two behaviours are worth stating out loud because they are easy to get backwards:
 *
 *  · `retrieval` arrives *before the model is called*, so the sources are painted while the answer
 *    is still being written. That is most of the perceived speed of this app, and it is why
 *    `phase` distinguishes `retrieving` from `streaming`.
 *  · a mid-stream `error` frame does **not** discard the partial answer. Contract 7 §8 says the
 *    server persists the message regardless of what the client does, so throwing away text the
 *    reader already saw would be both rude and inaccurate.
 */

import { useCallback, useRef, useState } from 'react';
import { useApi } from '../api/ApiProvider';
import { messageFor } from '../api/errors';
import type { AskStreamFn } from '../api/askStream';
import type { Citation, Message } from '../api/types';

export type AskPhase =
  | 'idle'
  /** Stream opened, `retrieval` not yet seen. */
  | 'retrieving'
  /** `retrieval` seen; tokens may or may not have started. */
  | 'streaming'
  | 'done'
  | 'error';

export interface AskState {
  phase: AskPhase;
  /** The question being answered, echoed into the thread immediately. */
  question: string | null;
  sources: Citation[];
  /** Contract 7 §5 — nothing cleared the similarity floor and Claude was never called. */
  insufficientContext: boolean;
  text: string;
  citationsUsed: number[];
  messageId: string | null;
  /** A sentence to render *beneath* whatever text already arrived. Never replaces it. */
  error: string | null;
}

const IDLE: AskState = {
  phase: 'idle',
  question: null,
  sources: [],
  insufficientContext: false,
  text: '',
  citationsUsed: [],
  messageId: null,
  error: null,
};

export interface AskParams {
  conversationId: string;
  question: string;
  /** Contract 7 §1 — `null` searches every document the caller owns. */
  documentIds: string[] | null;
}

export interface UseAskResult {
  state: AskState;
  /** Resolves when the stream is finished, however it finished. */
  ask(params: AskParams): Promise<void>;
  /** Abandon the reader. Contract 7 §8: the server finishes and persists the message anyway. */
  cancel(): void;
  reset(): void;
  /** The finished turn, ready to be appended to the thread. Null until `done`. */
  completedMessages(): [Message, Message] | null;
}

export function useAsk(streamOverride?: AskStreamFn): UseAskResult {
  const api = useApi();
  const stream = streamOverride ?? api.ask;
  const [state, setState] = useState<AskState>(IDLE);
  const controller = useRef<AbortController | null>(null);

  const cancel = useCallback(() => {
    controller.current?.abort();
    controller.current = null;
  }, []);

  const reset = useCallback(() => {
    cancel();
    setState(IDLE);
  }, [cancel]);

  const ask = useCallback(
    async ({ conversationId, question, documentIds }: AskParams) => {
      cancel();
      const abort = new AbortController();
      controller.current = abort;

      setState({ ...IDLE, phase: 'retrieving', question });

      try {
        const events = stream({
          conversationId,
          body: { question, document_ids: documentIds },
          signal: abort.signal,
        });

        for await (const event of events) {
          switch (event.event) {
            case 'retrieval':
              setState((s) => ({
                ...s,
                phase: 'streaming',
                sources: event.data.sources,
                insufficientContext: event.data.insufficient_context,
              }));
              break;

            case 'token':
              setState((s) => ({
                ...s,
                // Defensive: a token before `retrieval` would be a contract violation, but
                // rendering it is still better than dropping the answer on the floor.
                phase: s.phase === 'retrieving' ? 'streaming' : s.phase,
                text: s.text + event.data.text,
              }));
              break;

            case 'done':
              setState((s) => ({
                ...s,
                phase: 'done',
                messageId: event.data.message_id,
                citationsUsed: event.data.citations_used,
              }));
              break;

            case 'error':
              // Terminal, and it replaces `done` — but the partial text stays on screen.
              setState((s) => ({ ...s, phase: 'error', error: event.data.error }));
              break;
          }
        }

        // A body that closed without a terminal frame. Treated as an error so the reader is not
        // left watching a cursor blink forever.
        setState((s) =>
          s.phase === 'done' || s.phase === 'error'
            ? s
            : {
                ...s,
                phase: 'error',
                error: 'The answer stopped before it was finished. Please ask again.',
              },
        );
      } catch (cause) {
        if (abort.signal.aborted) return;
        setState((s) => ({ ...s, phase: 'error', error: messageFor(cause) }));
      } finally {
        if (controller.current === abort) controller.current = null;
      }
    },
    [stream, cancel],
  );

  const completedMessages = useCallback((): [Message, Message] | null => {
    if (state.phase !== 'done' || !state.messageId || state.question === null) return null;
    const now = new Date().toISOString();
    return [
      {
        // The user message is persisted server-side before the stream opens (Contract 7 §8); its
        // real id arrives on the next conversation fetch. This local id is display-only.
        id: `local-user-${state.messageId}`,
        role: 'user',
        content: state.question,
        citations: [],
        created_at: now,
      },
      {
        id: state.messageId,
        role: 'assistant',
        content: state.text,
        // Contract 7 §8: the stored message carries the full Citation array, which is exactly the
        // source list this answer was written from.
        citations: state.sources,
        created_at: now,
      },
    ];
  }, [state]);

  return { state, ask, cancel, reset, completedMessages };
}
