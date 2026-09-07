/** Small display helpers. All timestamps arriving from the API are ISO-8601 UTC with a `Z`. */

const dayFormat = new Intl.DateTimeFormat(undefined, {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
});

const timeFormat = new Intl.DateTimeFormat(undefined, {
  hour: 'numeric',
  minute: '2-digit',
});

export function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return dayFormat.format(date);
}

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return `${dayFormat.format(date)} · ${timeFormat.format(date)}`;
}

/**
 * Page chip copy — Contract 5 §5: `page_start`/`page_end` are 1-based and inclusive, and a chunk
 * spanning a page break carries both. `.txt`/`.md` have neither, and show no chip at all.
 */
export function formatPageRange(start: number | null, end: number | null): string | null {
  if (start === null && end === null) return null;
  if (start !== null && end !== null && end !== start) return `pp. ${start}–${end}`;
  const single = start ?? end;
  return single === null ? null : `p. ${single}`;
}

/** Similarity is 0..1, rounded to 3dp by the server. Shown as a percentage for debugging. */
export function formatSimilarity(similarity: number): string {
  return `${Math.round(similarity * 100)}%`;
}
