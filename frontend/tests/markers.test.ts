/**
 * `[n]` marker parsing — Contract 7 §7.
 *
 * Pure data in, pure data out. No DOM, no network, no component: the invariants that decide
 * whether a citation chip points at the right source are all expressible here.
 */

import { describe, expect, it } from 'vitest';
import { citedIndices, parseAnswer } from '../src/chat/markers';

describe('parseAnswer', () => {
  it('splits text around a marker', () => {
    expect(parseAnswer('Stratified by region [1].', { sourceCount: 2 })).toEqual([
      { kind: 'text', text: 'Stratified by region ' },
      { kind: 'citation', index: 1 },
      { kind: 'text', text: '.' },
    ]);
  });

  it('handles several markers, including adjacent ones', () => {
    const segments = parseAnswer('A [1][2] and B [3].', { sourceCount: 3 });

    expect(segments.filter((s) => s.kind === 'citation').map((s) => s.index)).toEqual([1, 2, 3]);
  });

  it('returns a single text segment when there are no markers', () => {
    expect(parseAnswer('No citations here.', { sourceCount: 2 })).toEqual([
      { kind: 'text', text: 'No citations here.' },
    ]);
  });

  it('leaves a marker with no corresponding source as literal text', () => {
    // The server strips these before they leave the process. If one ever arrives anyway, a chip
    // that opens nothing would be worse than showing what was actually written.
    const segments = parseAnswer('Claimed in [7].', { sourceCount: 2 });

    expect(segments).toEqual([{ kind: 'text', text: 'Claimed in [7].' }]);
  });

  it('leaves [0] as literal text — markers are 1-indexed', () => {
    expect(parseAnswer('See [0].', { sourceCount: 3 })).toEqual([
      { kind: 'text', text: 'See [0].' },
    ]);
  });

  it('accepts any index when the source count is unknown', () => {
    const segments = parseAnswer('See [9].');

    expect(segments).toContainEqual({ kind: 'citation', index: 9 });
  });

  it('withholds a half-written marker while tokens are still arriving', () => {
    // The reader must never watch a bare `[` or `[1` sit at the end of a sentence for a beat.
    expect(parseAnswer('Stratified by region [', { sourceCount: 2, streaming: true })).toEqual([
      { kind: 'text', text: 'Stratified by region ' },
    ]);
    expect(parseAnswer('Stratified by region [1', { sourceCount: 2, streaming: true })).toEqual([
      { kind: 'text', text: 'Stratified by region ' },
    ]);
  });

  it('renders the marker as soon as it closes', () => {
    expect(parseAnswer('Stratified by region [1]', { sourceCount: 2, streaming: true })).toEqual([
      { kind: 'text', text: 'Stratified by region ' },
      { kind: 'citation', index: 1 },
    ]);
  });

  it('does not withhold a trailing bracket once the stream has finished', () => {
    expect(parseAnswer('An open bracket [', { sourceCount: 2, streaming: false })).toEqual([
      { kind: 'text', text: 'An open bracket [' },
    ]);
  });

  it('never loses characters, marker or not', () => {
    const text = 'One [1] two [2] three [9] four.';
    const rebuilt = parseAnswer(text, { sourceCount: 2 })
      .map((s) => (s.kind === 'text' ? s.text : `[${s.index}]`))
      .join('');

    expect(rebuilt).toBe(text);
  });
});

describe('citedIndices', () => {
  it('lists distinct indices in first-appearance order', () => {
    expect(citedIndices('A [2] B [1] C [2] D [3].', 3)).toEqual([2, 1, 3]);
  });

  it('ignores markers that do not resolve to a source', () => {
    expect(citedIndices('A [1] B [8].', 2)).toEqual([1]);
  });

  it('returns nothing for an answer with no citations', () => {
    expect(citedIndices('Nothing cited.', 3)).toEqual([]);
  });
});
