/**
 * The inline `[n]` chip — the product, in eight square millimetres.
 *
 * It has to read as obviously interactive without shouting over the prose it sits inside, so it
 * borrows the marginalia vermilion and nothing else: no pill background at rest, no shadow, just
 * the number set in the mono face on a tinted ground, the way a printed footnote marker sits in
 * a line of type.
 *
 * Hover *or keyboard focus* reveals the snippet (Contract 8: `snippet` is for the hover card and
 * is never the source of truth). Activating it opens the real passage by `chunk_id`.
 */

import { useId } from 'react';
import type { Citation } from '../api/types';
import { formatPageRange } from '../ui/format';

export interface CitationChipProps {
  citation: Citation;
  onOpen: (citation: Citation) => void;
}

export function CitationChip({ citation, onOpen }: CitationChipProps) {
  const cardId = useId();
  const pages = formatPageRange(citation.page_start, citation.page_end);

  const label = [
    `Source ${citation.index}`,
    citation.filename,
    citation.heading_path ?? null,
    pages,
  ]
    .filter(Boolean)
    .join(', ');

  return (
    <span className="cite">
      <button
        type="button"
        className="cite__chip"
        onClick={() => onOpen(citation)}
        aria-label={`${label}. Open the passage.`}
        aria-describedby={cardId}
      >
        {citation.index}
      </button>

      <span className="cite__card" id={cardId} role="tooltip">
        <span className="cite__card-head">
          <span className="cite__card-file">{citation.filename}</span>
          {pages ? <span className="chip chip--page">{pages}</span> : null}
        </span>
        {citation.heading_path ? (
          <span className="cite__card-heading">{citation.heading_path}</span>
        ) : null}
        <span className="cite__card-snippet">{citation.snippet}</span>
      </span>
    </span>
  );
}
