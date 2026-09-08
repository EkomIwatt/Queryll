/**
 * Ingestion, made visible — Contract 3 §4 and §7.
 *
 * The three rules this component exists to hold:
 *
 *  1. `processing` at `progress === 0.0` is genuinely indeterminate. Parsing and chunking happen
 *     before any chunk count exists, so there is no fraction to show yet. A bar sitting at zero
 *     for forty seconds is indistinguishable from a hang, so it gets a moving bar instead.
 *  2. `failed` renders `error_message` **verbatim**. The worker writes that string as user-facing
 *     copy; replacing it with our own wording would throw away the only explanation there is.
 *  3. `progress` is monotonic within a run, so the bar never animates backwards.
 */

import type { Document } from '../api/types';
import { Progress } from '../ui/primitives';
import { AlertIcon, CheckIcon, ReindexIcon } from '../ui/icons';

export function statusLabel(document: Document): string {
  switch (document.status) {
    case 'pending':
      return 'Queued';
    case 'processing':
      return document.progress > 0 ? 'Embedding passages' : 'Reading the document';
    case 'ready':
      return 'Ready';
    case 'failed':
      return 'Could not be read';
  }
}

export function IngestionStatus({
  document,
  onRetry,
  retrying = false,
}: {
  document: Document;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  const label = statusLabel(document);

  if (document.status === 'ready') {
    return (
      <p className="ingest ingest--ready">
        <CheckIcon size={15} className="ingest__glyph" />
        <span>
          {document.chunk_count !== null ? (
            <>
              <strong>{document.chunk_count.toLocaleString()}</strong> passages indexed
            </>
          ) : (
            'Indexed'
          )}
          {document.page_count !== null ? ` · ${document.page_count} pages` : ''}
        </span>
      </p>
    );
  }

  if (document.status === 'failed') {
    return (
      <div className="ingest ingest--failed">
        <p className="ingest__failed-line">
          <AlertIcon size={15} className="ingest__glyph" />
          <span className="ingest__failed-label">{label}</span>
        </p>
        {/* Verbatim. The worker wrote this sentence for the reader, not for us. */}
        {document.error_message ? (
          <p className="ingest__message">{document.error_message}</p>
        ) : null}
        {onRetry ? (
          <button
            type="button"
            className="btn btn--secondary btn--small"
            onClick={onRetry}
            disabled={retrying}
          >
            <ReindexIcon size={14} />
            {retrying ? 'Retrying…' : 'Try again'}
          </button>
        ) : null}
      </div>
    );
  }

  const indeterminate = document.status === 'pending' || document.progress <= 0;
  const pct = Math.round(document.progress * 100);

  return (
    <div className="ingest ingest--busy">
      <p className="ingest__busy-line">
        <span className="ingest__pulse" aria-hidden="true" />
        <span>{label}</span>
        {!indeterminate ? <span className="ingest__pct">{pct}%</span> : null}
      </p>
      <Progress
        value={document.progress}
        indeterminate={indeterminate}
        label={`${label} — ${document.filename}`}
      />
    </div>
  );
}
