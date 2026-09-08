/**
 * The answer column.
 *
 * Prose is set in the reading serif at a generous measure and line-height, because the answer is
 * the thing the reader is here to read and everything else on the screen is apparatus. Paragraph
 * breaks in the streamed text are honoured; nothing else is interpreted as markup, because the
 * text comes from a model and rendering it as HTML would be a hole.
 */

import type { Citation } from '../api/types';
import { CitationChip } from './CitationChip';
import { parseAnswer } from './markers';

export interface AnswerTextProps {
  text: string;
  sources: readonly Citation[];
  /** True while tokens are still arriving — withholds a half-written `[n]` and shows the cursor. */
  streaming?: boolean;
  onOpenCitation: (citation: Citation) => void;
}

export function AnswerText({ text, sources, streaming = false, onOpenCitation }: AnswerTextProps) {
  const byIndex = new Map(sources.map((s) => [s.index, s]));
  const paragraphs = text.split(/\n{2,}/);

  return (
    <div className="answer">
      {paragraphs.map((paragraph, paragraphIndex) => {
        const isLast = paragraphIndex === paragraphs.length - 1;
        const segments = parseAnswer(paragraph, {
          sourceCount: sources.length,
          streaming: streaming && isLast,
        });

        return (
          // Paragraph order is stable and paragraphs have no identity of their own; the index is
          // the correct key here precisely because the list only ever grows at the end.
          <p className="answer__para" key={paragraphIndex}>
            {segments.map((segment, segmentIndex) => {
              if (segment.kind === 'text') {
                return <span key={segmentIndex}>{segment.text}</span>;
              }
              const citation = byIndex.get(segment.index);
              // Contract 7 §7 means this should be unreachable. If it ever fires, the marker is
              // shown as the literal text it was rather than as a chip that opens nothing.
              if (!citation) return <span key={segmentIndex}>[{segment.index}]</span>;
              return (
                <CitationChip key={segmentIndex} citation={citation} onOpen={onOpenCitation} />
              );
            })}
            {streaming && isLast ? <span className="answer__cursor" aria-hidden="true" /> : null}
          </p>
        );
      })}
    </div>
  );
}
