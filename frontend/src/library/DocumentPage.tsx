/**
 * A single document, and the passages it was split into — Contract 6 §3.
 *
 * This screen exists because chunking is the part of the system a reader has no other way to see.
 * Being able to page through the actual passages — with their heading paths and page ranges — is
 * how you find out that the splitter did something sensible, or that it did not.
 */

import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useApi } from '../api/ApiProvider';
import { messageFor } from '../api/errors';
import type { ChunkSummary, Document } from '../api/types';
import { PassageViewer } from '../passages/PassageViewer';
import type { PassageTarget } from '../passages/usePassage';
import { IngestionStatus } from './IngestionStatus';
import { formatBytes } from './uploadRules';
import { formatDateTime, formatPageRange } from '../ui/format';
import { ChevronLeftIcon } from '../ui/icons';
import { ErrorNotice, Spinner } from '../ui/primitives';

const CHUNK_PAGE_SIZE = 40;

export function DocumentPage() {
  const api = useApi();
  const { documentId = '' } = useParams<{ documentId: string }>();

  const [document, setDocument] = useState<Document | null>(null);
  const [chunks, setChunks] = useState<ChunkSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [passage, setPassage] = useState<PassageTarget | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void (async () => {
      try {
        const [doc, page] = await Promise.all([
          api.getDocument(documentId),
          api.listChunks(documentId, { limit: CHUNK_PAGE_SIZE }),
        ]);
        if (cancelled) return;
        setDocument(doc);
        setChunks(page.chunks);
        setCursor(page.next_cursor);
        setError(null);
      } catch (cause) {
        // A document belonging to someone else is a 404, never a 403 — nothing here reveals
        // whether the id exists.
        if (!cancelled) setError(messageFor(cause));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, documentId]);

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.listChunks(documentId, { limit: CHUNK_PAGE_SIZE, cursor });
      setChunks((current) => [...current, ...page.chunks]);
      setCursor(page.next_cursor);
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setLoadingMore(false);
    }
  }, [api, documentId, cursor, loadingMore]);

  return (
    <div className="docpage">
      <Link className="docpage__back" to="/library">
        <ChevronLeftIcon size={15} />
        Library
      </Link>

      {loading ? (
        <div className="docpage__loading">
          <Spinner label="Loading this document" size={20} />
        </div>
      ) : null}

      {error ? <ErrorNotice>{error}</ErrorNotice> : null}

      {document ? (
        <>
          <header className="docpage__head">
            <p className="eyebrow">Document</p>
            <h1 className="docpage__title">{document.filename}</h1>
            <p className="docpage__meta">
              <span>{formatBytes(document.size_bytes)}</span>
              <span aria-hidden="true">·</span>
              <span>Added {formatDateTime(document.created_at)}</span>
              {document.indexed_at ? (
                <>
                  <span aria-hidden="true">·</span>
                  <span>Indexed {formatDateTime(document.indexed_at)}</span>
                </>
              ) : null}
            </p>
            <div className="docpage__status">
              <IngestionStatus document={document} />
            </div>
          </header>

          {chunks.length > 0 ? (
            <>
              <p className="eyebrow docpage__section">Passages</p>
              <ol className="passages">
                {chunks.map((chunk) => {
                  const pages = formatPageRange(chunk.page_start, chunk.page_end);
                  return (
                    <li key={chunk.id}>
                      <button
                        type="button"
                        className="passages__item"
                        onClick={() =>
                          setPassage({
                            documentId,
                            chunkId: chunk.id,
                            filename: document.filename,
                            snippet: chunk.preview,
                            headingPath: chunk.heading_path,
                            pageStart: chunk.page_start,
                            pageEnd: chunk.page_end,
                          })
                        }
                      >
                        <span className="passages__ordinal">{chunk.ordinal + 1}</span>
                        <span className="passages__body">
                          {chunk.heading_path ? (
                            <span className="passages__heading">{chunk.heading_path}</span>
                          ) : null}
                          <span className="passages__preview">{chunk.preview}</span>
                          {pages ? <span className="chip chip--page">{pages}</span> : null}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ol>

              {cursor ? (
                <div className="docpage__more">
                  <button
                    type="button"
                    className="btn btn--secondary"
                    onClick={() => void loadMore()}
                    disabled={loadingMore}
                  >
                    {loadingMore ? <Spinner label="Loading" /> : null}
                    Load more passages
                  </button>
                </div>
              ) : null}
            </>
          ) : null}
        </>
      ) : null}

      <PassageViewer target={passage} onClose={() => setPassage(null)} />
    </div>
  );
}
