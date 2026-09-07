/**
 * Client-side upload validation — Contract 5 §1 and Contract 6 §1.
 *
 * This exists so a 25 MB file is refused instantly instead of after a 30-second upload that ends
 * in a 413. It is a courtesy, not a gate: the server's rejection is always authoritative, and the
 * UI renders the server's sentence over its own whenever both exist.
 *
 * Pure functions, no DOM — tested without a component.
 */

import { ACCEPTED_EXTENSIONS, ACCEPTED_MIME_TYPES, MAX_UPLOAD_BYTES } from '../api/types';

export type UploadRejection = { ok: false; reason: string };
export type UploadAcceptance = { ok: true };
export type UploadVerdict = UploadAcceptance | UploadRejection;

/** What to put in the file input's `accept` attribute. */
export const UPLOAD_ACCEPT_ATTR = [...ACCEPTED_MIME_TYPES, ...ACCEPTED_EXTENSIONS].join(',');

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  const mb = bytes / (1024 * 1024);
  return `${mb < 10 ? mb.toFixed(1) : mb.toFixed(0)} MB`;
}

function hasAcceptedExtension(name: string): boolean {
  const lower = name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

/**
 * Browsers disagree about `.md`: Chrome reports `text/markdown`, Safari reports `text/plain`, and
 * several report an empty string. So the extension is accepted as corroboration when the reported
 * type is empty or unrecognised — the server sniffs the actual content either way.
 */
export function checkUpload(file: { name: string; size: number; type: string }): UploadVerdict {
  if (file.size === 0) {
    return { ok: false, reason: `${file.name} is empty. There is nothing in it to read.` };
  }

  if (file.size > MAX_UPLOAD_BYTES) {
    return {
      ok: false,
      reason: `${file.name} is ${formatBytes(file.size)}. The limit is ${formatBytes(
        MAX_UPLOAD_BYTES,
      )}.`,
    };
  }

  const declared = file.type.split(';')[0]?.trim().toLowerCase() ?? '';
  const typeAccepted = (ACCEPTED_MIME_TYPES as readonly string[]).includes(declared);

  if (!typeAccepted && !(declared === '' && hasAcceptedExtension(file.name))) {
    // An unrecognised declared type still passes if the extension corroborates it, because the
    // server sniffs content and is the real authority.
    if (!hasAcceptedExtension(file.name)) {
      return {
        ok: false,
        reason: `${file.name} is not a supported file type. Upload a PDF, .txt or .md file.`,
      };
    }
  }

  return { ok: true };
}
