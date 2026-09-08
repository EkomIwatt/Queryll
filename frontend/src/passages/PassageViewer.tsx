/**
 * The passage viewer.
 *
 * This is where a citation gets *checked*, so the source text is the hero: the cited passage is
 * set in the reading serif at full measure with a highlighter pass behind it, and its immediate
 * neighbours sit above and below in a lighter tone so the reader can see it in context without
 * losing track of which words the answer actually came from.
 *
 * The neighbours come from Contract 6 §3's `prev`/`next` — that route exists precisely so this
 * dialog does not have to fetch a whole document to show three paragraphs.
 */

import { Modal, ErrorNotice, Spinner } from '../ui/primitives';
import { ChevronLeftIcon, ChevronRightIcon, QuoteIcon } from '../ui/icons';
import { formatPageRange } from '../ui/format';
import { usePassage, type PassageTarget } from './usePassage';

export interface PassageViewerProps {
  target: PassageTarget | null;
  onClose: () => void;
}

export function PassageViewer({ target, onClose }: PassageViewerProps) {
  const { state, goTo } = usePassage(target);

  const loaded = state.status === 'loaded' ? state : null;
  const pageRange = loaded
    ? formatPageRange(loaded.chunk.page_start, loaded.chunk.page_end)
    : formatPageRange(target?.pageStart ?? null, target?.pageEnd ?? null);
  const headingPath = loaded ? loaded.chunk.heading_path : (target?.headingPath ?? null);

  return (
    <Modal
      open={target !== null}
      onClose={onClose}
      title={target ? `Passage from ${target.filename}` : 'Passage'}
      className="modal--passage"
      toolbar={
        <div className="passage__identity">
          <p className="eyebrow">Source passage</p>
          <h2 className="passage__filename">{target?.filename}</h2>
          <p className="passage__crumbs">
            {headingPath ? <span className="passage__heading">{headingPath}</span> : null}
            {pageRange ? <span className="chip chip--page">{pageRange}</span> : null}
            {loaded ? (
              <span className="chip chip--ordinal">passage {loaded.chunk.ordinal + 1}</span>
            ) : null}
          </p>
        </div>
      }
    >
      {state.status === 'loading' ? (
        <div className="passage__loading">
          <Spinner label="Opening the passage" size={20} />
        </div>
      ) : null}

      {state.status === 'error' ? <ErrorNotice>{state.message}</ErrorNotice> : null}

      {/* Contract 8 §3 — do not error, and do not hide the citation. Show what it was based on. */}
      {state.status === 'missing' ? (
        <div className="passage__missing">
          <p className="passage__missing-note">
            This source is no longer available — the document has been deleted or re-indexed since
            this answer was written. The passage the answer quoted is below.
          </p>
          {target?.snippet ? (
            <blockquote className="passage__snippet">
              <QuoteIcon size={18} className="passage__quote-glyph" />
              {target.snippet}
            </blockquote>
          ) : null}
        </div>
      ) : null}

      {loaded ? (
        <div className="passage__reader">
          {loaded.prev ? (
            <button
              type="button"
              className="passage__neighbour passage__neighbour--prev"
              onClick={() => goTo(loaded.prev!.id)}
            >
              <span className="passage__neighbour-label">
                <ChevronLeftIcon size={14} />
                Passage before
              </span>
              <p className="passage__neighbour-text">{loaded.prev.text}</p>
            </button>
          ) : (
            <p className="passage__edge">Start of the document</p>
          )}

          <div className="passage__focus">
            <p className="passage__text">{loaded.chunk.text}</p>
            <p className="passage__tokens">{loaded.chunk.token_count} tokens</p>
          </div>

          {loaded.next ? (
            <button
              type="button"
              className="passage__neighbour passage__neighbour--next"
              onClick={() => goTo(loaded.next!.id)}
            >
              <span className="passage__neighbour-label">
                Passage after
                <ChevronRightIcon size={14} />
              </span>
              <p className="passage__neighbour-text">{loaded.next.text}</p>
            </button>
          ) : (
            <p className="passage__edge">End of the document</p>
          )}
        </div>
      ) : null}
    </Modal>
  );
}
