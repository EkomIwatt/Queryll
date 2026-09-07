/**
 * Upload validation — Contract 5 §1 and Contract 6 §1, as pure data.
 *
 * These rules are a courtesy to the reader, not a security boundary: the server sniffs content and
 * is always the authority. The point of testing them here is that the *courtesy* is right — an
 * instant, accurate sentence instead of a thirty-second upload ending in a 413.
 */

import { describe, expect, it } from 'vitest';
import { MAX_UPLOAD_BYTES } from '../src/api/types';
import { checkUpload, formatBytes, UPLOAD_ACCEPT_ATTR } from '../src/library/uploadRules';

const file = (name: string, size: number, type: string) => ({ name, size, type });

describe('checkUpload', () => {
  it('accepts the three supported types', () => {
    expect(checkUpload(file('paper.pdf', 1_000, 'application/pdf')).ok).toBe(true);
    expect(checkUpload(file('notes.txt', 1_000, 'text/plain')).ok).toBe(true);
    expect(checkUpload(file('readme.md', 1_000, 'text/markdown')).ok).toBe(true);
  });

  it('accepts a .md file whose type the browser did not recognise', () => {
    // Chrome says `text/markdown`, Safari says `text/plain`, others say nothing at all.
    expect(checkUpload(file('readme.md', 1_000, '')).ok).toBe(true);
    expect(checkUpload(file('readme.markdown', 1_000, '')).ok).toBe(true);
  });

  it('ignores a charset parameter on the declared type', () => {
    expect(checkUpload(file('notes.txt', 10, 'text/plain; charset=utf-8')).ok).toBe(true);
  });

  it('rejects an unsupported type, naming the file and what is allowed', () => {
    const verdict = checkUpload(file('slides.pptx', 1_000, 'application/vnd.ms-powerpoint'));

    expect(verdict).toEqual({
      ok: false,
      reason: 'slides.pptx is not a supported file type. Upload a PDF, .txt or .md file.',
    });
  });

  it('rejects an empty file', () => {
    expect(checkUpload(file('empty.pdf', 0, 'application/pdf'))).toEqual({
      ok: false,
      reason: 'empty.pdf is empty. There is nothing in it to read.',
    });
  });

  it('accepts a file exactly at the 20 MB cap and rejects one byte more', () => {
    expect(checkUpload(file('big.pdf', MAX_UPLOAD_BYTES, 'application/pdf')).ok).toBe(true);

    const verdict = checkUpload(file('bigger.pdf', MAX_UPLOAD_BYTES + 1, 'application/pdf'));
    expect(verdict.ok).toBe(false);
    expect(verdict.ok === false && verdict.reason).toContain('The limit is 20 MB.');
  });

  it('checks emptiness before size, so a 0-byte file gets the accurate reason', () => {
    expect(checkUpload(file('empty.exe', 0, 'application/x-msdownload')).ok).toBe(false);
    expect(checkUpload(file('empty.exe', 0, 'application/x-msdownload'))).toMatchObject({
      reason: expect.stringContaining('is empty'),
    });
  });
});

describe('formatBytes', () => {
  it('formats across the ranges the UI actually shows', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(1_500_000)).toBe('1.4 MB');
    expect(formatBytes(MAX_UPLOAD_BYTES)).toBe('20 MB');
  });
});

describe('UPLOAD_ACCEPT_ATTR', () => {
  it('offers both the contract MIME types and the extensions browsers actually match on', () => {
    expect(UPLOAD_ACCEPT_ATTR).toContain('application/pdf');
    expect(UPLOAD_ACCEPT_ATTR).toContain('.md');
  });
});
