/**
 * Resolving a citation to its passage — Contract 6 §3 and Contract 8 §2/§3.
 *
 * A citation is resolved by `chunk_id` and by nothing else: never by index, never by page, never
 * by matching the snippet text. `index` is per-answer, pages are display metadata, and the snippet
 * is a hover card — only `chunk_id` is the stable handle, and using anything else is how a UI ends
 * up confidently opening the wrong passage.
 *
 * A 404 here is an ordinary, expected outcome (Contract 8 §3): the document has since been deleted
 * or re-indexed, and re-indexing mints new chunk ids. That is `missing`, not `error` — a historical
 * answer keeps showing what it was based on.
 */

import { useCallback, useEffect, useState } from 'react';
import { useApi } from '../api/ApiProvider';
import { ApiError, messageFor } from '../api/errors';
import type { Chunk, Citation } from '../api/types';

/** Everything needed to open a passage, plus the fallback display data for a dangling one. */
export interface PassageTarget {
  documentId: string;
  chunkId: string;
  filename: string;
  snippet?: string;
  headingPath?: string | null;
  pageStart?: number | null;
  pageEnd?: number | null;
}

export function targetFromCitation(citation: Citation): PassageTarget {
  return {
    documentId: citation.document_id,
    chunkId: citation.chunk_id,
    filename: citation.filename,
    snippet: citation.snippet,
    headingPath: citation.heading_path,
    pageStart: citation.page_start,
    pageEnd: citation.page_end,
  };
}

export type PassageState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'loaded'; chunk: Chunk; prev: Chunk | null; next: Chunk | null }
  /** Contract 8 §3 — the document was deleted or re-indexed. Not an error. */
  | { status: 'missing' }
  | { status: 'error'; message: string };

export interface UsePassageResult {
  state: PassageState;
  /** The chunk currently in view — changes as the reader walks to a neighbour. */
  chunkId: string | null;
  /** Walk to a neighbouring passage without leaving the viewer. */
  goTo(chunkId: string): void;
  reload(): void;
}

export function usePassage(target: PassageTarget | null): UsePassageResult {
  const api = useApi();
  const [chunkId, setChunkId] = useState<string | null>(target?.chunkId ?? null);
  const [state, setState] = useState<PassageState>({ status: 'idle' });
  const [reloadCount, setReloadCount] = useState(0);

  // A new target resets the view to that citation's own chunk.
  useEffect(() => {
    setChunkId(target?.chunkId ?? null);
  }, [target?.documentId, target?.chunkId]);

  useEffect(() => {
    if (!target || !chunkId) {
      setState({ status: 'idle' });
      return;
    }

    let cancelled = false;
    setState({ status: 'loading' });

    void (async () => {
      try {
        const detail = await api.getChunk(target.documentId, chunkId);
        if (cancelled) return;
        setState({ status: 'loaded', chunk: detail.chunk, prev: detail.prev, next: detail.next });
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof ApiError && cause.isNotFound) {
          setState({ status: 'missing' });
        } else {
          setState({ status: 'error', message: messageFor(cause) });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [api, target, chunkId, reloadCount]);

  const goTo = useCallback((next: string) => setChunkId(next), []);
  const reload = useCallback(() => setReloadCount((n) => n + 1), []);

  return { state, chunkId, goTo, reload };
}
