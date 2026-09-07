/**
 * The sources apparatus — Contract 7 §3.
 *
 * `retrieval` is the first frame on the wire, sent before the model is called, so these cards are
 * painted while the answer is still being written. That is deliberate and it is most of what makes
 * the app feel fast: the reader can see what is being read from before there is anything to read.
 *
 * Once `done` arrives, `citations_used` says which of them the answer actually referenced. The
 * ones it did not are dimmed rather than removed — "we retrieved this and did not need it" is
 * useful information, and hiding it would quietly overstate how well retrieval did.
 */

import type { Citation } from '../api/types';
import { formatPageRange, formatSimilarity } from '../ui/format';

export interface SourcesPanelProps {
  sources: readonly Citation[];
  /** 1-based indices from `done`. Null while the answer is still streaming. */
  citationsUsed: readonly number[] | null;
  onOpen: (citation: Citation) => void;
}

export function SourcesPanel({ sources, citationsUsed, onOpen }: SourcesPanelProps) {
  if (sources.length === 0) return null;

  const used = citationsUsed === null ? null : new Set(citationsUsed);

  return (
    <aside className="sources" aria-label="Passages this answer was drawn from">
      <p className="eyebrow sources__label">
        Reading from {sources.length} passage{sources.length === 1 ? '' : 's'}
      </p>

      <ol className="sources__list">
        {sources.map((source) => {
          const unused = used !== null && !used.has(source.index);
          const pages = formatPageRange(source.page_start, source.page_end);

          return (
            <li key={source.chunk_id}>
              <button
                type="button"
                className={`source${unused ? ' source--unused' : ''}`}
                onClick={() => onOpen(source)}
              >
                <span className="source__index">{source.index}</span>
                <span className="source__body">
                  <span className="source__file">{source.filename}</span>
                  {source.heading_path ? (
                    <span className="source__heading">{source.heading_path}</span>
                  ) : null}
                  <span className="source__snippet">{source.snippet}</span>
                  <span className="source__meta">
                    {pages ? <span className="chip chip--page">{pages}</span> : null}
                    <span
                      className="chip chip--score"
                      title="Cosine similarity between your question and this passage"
                    >
                      {formatSimilarity(source.similarity)}
                    </span>
                    {unused ? <span className="source__unused-note">not cited</span> : null}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
