/**
 * The document library, and the polling discipline — Contract 6 §4.
 *
 * The rule is exact and it is the kind of thing nobody notices is wrong until a rate limit or a
 * bill arrives, so it is stated once here and tested with fake timers:
 *
 *   · poll `GET /api/documents` every 2s while ANY document is `pending` or `processing`;
 *   · stop entirely the moment none is — an idle library tab issues zero requests;
 *   · after 5 minutes of *continuous* polling, back the interval off to 10s.
 *
 * Scheduling is `setTimeout`-after-response rather than `setInterval`, so a slow API can never
 * stack overlapping requests, and the 5-minute clock starts when polling starts and resets to
 * null the moment it stops.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApi } from '../api/ApiProvider';
import { messageFor } from '../api/errors';
import type { Document } from '../api/types';

export const POLL_INTERVAL_MS = 2_000;
export const POLL_SLOW_INTERVAL_MS = 10_000;
export const POLL_BACKOFF_AFTER_MS = 5 * 60 * 1_000;
export const PAGE_SIZE = 50;

/** Contract 3 §4 — the two statuses that mean the worker still has work to do. */
export function isInFlight(document: Document): boolean {
  return document.status === 'pending' || document.status === 'processing';
}

/**
 * Merge a freshly-fetched first page into the list we already hold.
 *
 * Replacing the whole list would drop any later pages the reader has loaded, and appending would
 * duplicate. Both were bugs worth avoiding by construction, so the merge is a named, pure function.
 */
export function mergeDocuments(existing: readonly Document[], page: readonly Document[]): Document[] {
  const incoming = new Map(page.map((doc) => [doc.id, doc]));
  const merged: Document[] = [];

  for (const doc of page) merged.push(doc);
  for (const doc of existing) {
    if (!incoming.has(doc.id)) merged.push(doc);
  }
  return merged;
}

export type LibraryStatus = 'loading' | 'ready' | 'error';

export interface UseDocumentsResult {
  documents: Document[];
  status: LibraryStatus;
  /** A server sentence, rendered verbatim. */
  error: string | null;
  /** True while a poll cycle is scheduled — surfaced in the UI as a quiet "watching" note. */
  polling: boolean;
  nextCursor: string | null;
  loadingMore: boolean;
  refresh(): Promise<void>;
  loadMore(): Promise<void>;
  /** Optimistically place an accepted upload at the top; polling takes it from there. */
  adopt(document: Document): void;
  reindex(id: string): Promise<void>;
  remove(id: string): Promise<void>;
}

export function useDocuments(): UseDocumentsResult {
  const api = useApi();

  const [documents, setDocuments] = useState<Document[]>([]);
  const [status, setStatus] = useState<LibraryStatus>('loading');
  const [error, setError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  /** Bumped after every completed fetch attempt; it is what re-arms the poll timer. */
  const [tick, setTick] = useState(0);
  const pollingSince = useRef<number | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const fetchFirstPage = useCallback(
    async (options: { initial?: boolean } = {}) => {
      try {
        const page = await api.listDocuments({ limit: PAGE_SIZE });
        if (!mounted.current) return;
        setDocuments((current) =>
          options.initial ? page.documents : mergeDocuments(current, page.documents),
        );
        if (options.initial) setNextCursor(page.next_cursor);
        setError(null);
        setStatus('ready');
      } catch (cause) {
        if (!mounted.current) return;
        setError(messageFor(cause));
        // A failed poll must not blank a library that is already on screen.
        setStatus((current) => (current === 'ready' ? 'ready' : 'error'));
      } finally {
        if (mounted.current) setTick((n) => n + 1);
      }
    },
    [api],
  );

  useEffect(() => {
    void fetchFirstPage({ initial: true });
  }, [fetchFirstPage]);

  const needsPolling = useMemo(() => documents.some(isInFlight), [documents]);

  useEffect(() => {
    if (!needsPolling) {
      // Nothing in flight: the clock resets and, crucially, no timer is armed at all.
      pollingSince.current = null;
      return;
    }

    pollingSince.current ??= Date.now();
    const elapsed = Date.now() - pollingSince.current;
    const delay = elapsed >= POLL_BACKOFF_AFTER_MS ? POLL_SLOW_INTERVAL_MS : POLL_INTERVAL_MS;

    const timer = setTimeout(() => {
      void fetchFirstPage();
    }, delay);

    return () => clearTimeout(timer);
    // `tick` re-arms the timer after each completed poll, including a failed one.
  }, [needsPolling, tick, fetchFirstPage]);

  const refresh = useCallback(() => fetchFirstPage(), [fetchFirstPage]);

  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.listDocuments({ limit: PAGE_SIZE, cursor: nextCursor });
      if (!mounted.current) return;
      setDocuments((current) => [...current, ...page.documents]);
      setNextCursor(page.next_cursor);
      setError(null);
    } catch (cause) {
      if (mounted.current) setError(messageFor(cause));
    } finally {
      if (mounted.current) setLoadingMore(false);
    }
  }, [api, nextCursor, loadingMore]);

  const adopt = useCallback((document: Document) => {
    setDocuments((current) => [document, ...current.filter((d) => d.id !== document.id)]);
  }, []);

  const reindex = useCallback(
    async (id: string) => {
      // Contract 6 §5: 202 with the document reset to `pending`, or 409 if it is processing.
      const updated = await api.reindexDocument(id);
      if (!mounted.current) return;
      setDocuments((current) => current.map((d) => (d.id === id ? updated : d)));
      setError(null);
    },
    [api],
  );

  const remove = useCallback(
    async (id: string) => {
      await api.deleteDocument(id);
      if (!mounted.current) return;
      setDocuments((current) => current.filter((d) => d.id !== id));
      setError(null);
    },
    [api],
  );

  return {
    documents,
    status,
    error,
    polling: needsPolling,
    nextCursor,
    loadingMore,
    refresh,
    loadMore,
    adopt,
    reindex,
    remove,
  };
}
