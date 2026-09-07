/**
 * Turning answer text into text-and-citations — Contract 7 §7.
 *
 * The server is the authority on marker validity: it validates every `[n]` against the sources it
 * actually sent and strips anything out of range *before* the token leaves the process, and it
 * buffers a trailing partial marker so a half-marker never crosses the wire. So by the time text
 * reaches here, every `[n]` should resolve.
 *
 * This parser still refuses to render a marker it cannot resolve, for two reasons: a chip that
 * opens nothing is worse than plain text, and a client that renders whatever it is handed cannot
 * tell anyone when the two sides have drifted.
 *
 * Pure. No React, no DOM — the invariants below are all testable as data.
 */

export type AnswerSegment =
  | { kind: 'text'; text: string }
  | { kind: 'citation'; index: number };

const MARKER = /\[(\d{1,3})\]/g;
/** A marker that has begun but not closed, sitting at the very end of the text so far. */
const PARTIAL_TRAILING_MARKER = /\[\d{0,3}$/;

export interface ParseAnswerOptions {
  /**
   * How many sources the `retrieval` event carried. A marker outside `1..sourceCount` is rendered
   * as literal text rather than as a chip that would resolve to nothing.
   * Omit to accept any marker (used when the source list is not known to the caller).
   */
  sourceCount?: number;
  /**
   * True while tokens are still arriving. Holds back a trailing partial marker so the reader never
   * watches a bare `[` sit at the end of the sentence for a beat before it becomes `[2]`.
   */
  streaming?: boolean;
}

export function parseAnswer(text: string, options: ParseAnswerOptions = {}): AnswerSegment[] {
  const source = options.streaming ? text.replace(PARTIAL_TRAILING_MARKER, '') : text;
  const segments: AnswerSegment[] = [];
  let cursor = 0;

  MARKER.lastIndex = 0;
  for (let match = MARKER.exec(source); match !== null; match = MARKER.exec(source)) {
    const index = Number(match[1]);
    const resolvable =
      index >= 1 && (options.sourceCount === undefined || index <= options.sourceCount);

    if (!resolvable) continue; // leave it in the surrounding text run, exactly as written

    if (match.index > cursor) {
      segments.push({ kind: 'text', text: source.slice(cursor, match.index) });
    }
    segments.push({ kind: 'citation', index });
    cursor = match.index + match[0].length;
  }

  if (cursor < source.length) {
    segments.push({ kind: 'text', text: source.slice(cursor) });
  }

  return segments;
}

/** The distinct citation indices an answer actually references, in first-appearance order. */
export function citedIndices(text: string, sourceCount?: number): number[] {
  const seen = new Set<number>();
  const order: number[] = [];
  for (const segment of parseAnswer(text, sourceCount === undefined ? {} : { sourceCount })) {
    if (segment.kind === 'citation' && !seen.has(segment.index)) {
      seen.add(segment.index);
      order.push(segment.index);
    }
  }
  return order;
}
