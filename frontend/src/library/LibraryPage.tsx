/**
 * The document library — Contract 6.
 *
 * The first-run empty state is treated as a real screen rather than a shrug: it is the moment that
 * has to teach someone what Queryll is, and it is the only screen every new account sees.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useDocuments } from './useDocuments';
import { Uploader } from './Uploader';
import { DocumentCard } from './DocumentCard';
import { messageFor } from '../api/errors';
import { EmptyState, ErrorNotice, Modal, Spinner } from '../ui/primitives';
import { AskIcon } from '../ui/icons';
import type { Document } from '../api/types';

export function LibraryPage() {
  const library = useDocuments();
  const [pendingDelete, setPendingDelete] = useState<Document | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const readyCount = library.documents.filter((d) => d.status === 'ready').length;

  async function run(id: string, action: () => Promise<void>) {
    setBusyId(id);
    setActionError(null);
    try {
      await action();
    } catch (cause) {
      // Includes the 409 the API returns for a delete or re-index racing the worker.
      setActionError(messageFor(cause));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="library">
      <header className="library__head">
        <div>
          <p className="eyebrow">Library</p>
          <h1 className="library__title">Your documents</h1>
          <p className="library__lede">
            {library.documents.length === 0
              ? 'Everything Queryll can answer from lives here.'
              : `${library.documents.length} document${
                  library.documents.length === 1 ? '' : 's'
                } · ${readyCount} ready to search`}
          </p>
        </div>

        {readyCount > 0 ? (
          <Link className="btn btn--primary" to="/ask">
            <AskIcon size={17} />
            Ask a question
          </Link>
        ) : null}
      </header>

      <Uploader onAccepted={library.adopt} compact={library.documents.length > 0} />

      {library.error ? <ErrorNotice>{library.error}</ErrorNotice> : null}
      {actionError ? <ErrorNotice>{actionError}</ErrorNotice> : null}

      {library.status === 'loading' ? (
        <div className="library__loading">
          <Spinner label="Loading your documents" size={20} />
        </div>
      ) : null}

      {library.status !== 'loading' && library.documents.length === 0 ? (
        <EmptyState
          eyebrow="First run"
          title="Give Queryll something to read."
          action={
            <ol className="onboarding">
              <li>
                <span className="onboarding__num">1</span>
                <div>
                  <h3>Upload a document</h3>
                  <p>
                    A PDF, a plain-text file or a Markdown file, up to 20 MB. A paper, a contract,
                    a handbook — anything you would otherwise be scrolling through.
                  </p>
                </div>
              </li>
              <li>
                <span className="onboarding__num">2</span>
                <div>
                  <h3>Wait a few seconds</h3>
                  <p>
                    Queryll splits it on its real structure — headings, paragraphs, sentences — and
                    indexes each passage. You will watch it happen here.
                  </p>
                </div>
              </li>
              <li>
                <span className="onboarding__num">3</span>
                <div>
                  <h3>Ask, and check the answer</h3>
                  <p>
                    Every claim carries a numbered citation. Click one and the passage opens in
                    context, with its page. If the answer is not in your documents, Queryll says so
                    rather than guessing.
                  </p>
                </div>
              </li>
            </ol>
          }
        />
      ) : null}

      {library.documents.length > 0 ? (
        <ul className="library__list">
          {library.documents.map((document) => (
            <li key={document.id}>
              <DocumentCard
                document={document}
                busy={busyId === document.id}
                onReindex={() => void run(document.id, () => library.reindex(document.id))}
                onDelete={() => setPendingDelete(document)}
              />
            </li>
          ))}
        </ul>
      ) : null}

      {library.nextCursor ? (
        <div className="library__more">
          <button
            type="button"
            className="btn btn--secondary"
            onClick={() => void library.loadMore()}
            disabled={library.loadingMore}
          >
            {library.loadingMore ? <Spinner label="Loading" /> : null}
            Load older documents
          </button>
        </div>
      ) : null}

      <Modal
        open={pendingDelete !== null}
        onClose={() => setPendingDelete(null)}
        title="Delete document"
        toolbar={<h2 className="modal__title">Delete this document?</h2>}
        className="modal--confirm"
      >
        <p className="confirm__body">
          <strong>{pendingDelete?.filename}</strong> and all of its indexed passages will be
          removed. Answers you have already received will keep showing what they were based on,
          but their citations will no longer open.
        </p>
        <div className="confirm__actions">
          <button
            type="button"
            className="btn btn--secondary"
            onClick={() => setPendingDelete(null)}
          >
            Keep it
          </button>
          <button
            type="button"
            className="btn btn--danger"
            onClick={() => {
              const target = pendingDelete;
              setPendingDelete(null);
              if (target) void run(target.id, () => library.remove(target.id));
            }}
          >
            Delete permanently
          </button>
        </div>
      </Modal>
    </div>
  );
}
