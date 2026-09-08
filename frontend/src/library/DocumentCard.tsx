import { Link } from 'react-router-dom';
import type { Document } from '../api/types';
import { IngestionStatus } from './IngestionStatus';
import { formatBytes } from './uploadRules';
import { formatDate } from '../ui/format';
import { DocumentIcon, ReindexIcon, TrashIcon } from '../ui/icons';

export interface DocumentCardProps {
  document: Document;
  busy: boolean;
  onReindex: () => void;
  onDelete: () => void;
}

export function DocumentCard({ document, busy, onReindex, onDelete }: DocumentCardProps) {
  // Contract 6 §5/§6: both actions are refused with 409 while the worker holds the document, so
  // they are disabled here too. The server is still the authority; this only avoids a pointless
  // round-trip and an error the reader could have been spared.
  const locked = document.status === 'processing';
  const browsable = document.status === 'ready';

  return (
    <article className={`doc doc--${document.status}`}>
      <div className="doc__main">
        <span className="doc__icon" aria-hidden="true">
          <DocumentIcon size={18} />
        </span>

        <div className="doc__identity">
          <h2 className="doc__name">
            {browsable ? (
              <Link to={`/library/${document.id}`}>{document.filename}</Link>
            ) : (
              document.filename
            )}
          </h2>
          <p className="doc__meta">
            <span>{formatBytes(document.size_bytes)}</span>
            <span aria-hidden="true">·</span>
            <span>Added {formatDate(document.created_at)}</span>
          </p>
        </div>

        <div className="doc__actions">
          <button
            type="button"
            className="btn btn--ghost btn--small"
            onClick={onReindex}
            disabled={locked || busy}
            title={locked ? 'Available once processing finishes' : 'Re-index this document'}
            aria-label={`Re-index ${document.filename}`}
          >
            <ReindexIcon size={15} />
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--small doc__delete"
            onClick={onDelete}
            disabled={locked || busy}
            title={locked ? 'Available once processing finishes' : 'Delete this document'}
            aria-label={`Delete ${document.filename}`}
          >
            <TrashIcon size={15} />
          </button>
        </div>
      </div>

      <div className="doc__status">
        <IngestionStatus document={document} onRetry={onReindex} retrying={busy} />
      </div>
    </article>
  );
}
