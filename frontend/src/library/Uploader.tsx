/**
 * The uploader — Contract 6 §1.
 *
 * A drop zone that is also a real `<button>` wrapping a real `<input type="file">`, so it is
 * reachable and operable from the keyboard without any of the usual div-with-a-click-handler
 * theatre. Drag-and-drop is the enhancement, not the mechanism.
 *
 * Files are validated locally first (instant answer instead of a round-trip to a 413), then
 * uploaded one at a time. The server's rejection always wins over ours.
 */

import { useCallback, useRef, useState, type ChangeEvent, type DragEvent } from 'react';
import { useApi } from '../api/ApiProvider';
import { messageFor } from '../api/errors';
import type { Document } from '../api/types';
import { MAX_UPLOAD_BYTES } from '../api/types';
import { UPLOAD_ACCEPT_ATTR, checkUpload, formatBytes } from './uploadRules';
import { Spinner } from '../ui/primitives';
import { UploadIcon } from '../ui/icons';

export interface UploaderProps {
  /** Called with each `202 Accepted` document, in upload order. */
  onAccepted: (document: Document) => void;
  compact?: boolean;
}

export function Uploader({ onAccepted, compact = false }: UploaderProps) {
  const api = useApi();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [rejections, setRejections] = useState<string[]>([]);

  const send = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;

      const problems: string[] = [];
      const accepted: File[] = [];
      for (const file of files) {
        const verdict = checkUpload(file);
        if (verdict.ok) accepted.push(file);
        else problems.push(verdict.reason);
      }
      setRejections(problems);
      if (accepted.length === 0) return;

      setBusy(true);
      try {
        // Sequential: the API returns 202 immediately, and serialising keeps the library order
        // predictable rather than racing N uploads into an arbitrary arrangement.
        for (const file of accepted) {
          try {
            onAccepted(await api.uploadDocument(file));
          } catch (cause) {
            problems.push(messageFor(cause));
            setRejections([...problems]);
          }
        }
      } finally {
        setBusy(false);
      }
    },
    [api, onAccepted],
  );

  const handleInput = (event: ChangeEvent<HTMLInputElement>) => {
    void send(Array.from(event.target.files ?? []));
    // Reset so re-selecting the same file fires `change` again.
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    void send(Array.from(event.dataTransfer.files));
  };

  return (
    <div className={`uploader${compact ? ' uploader--compact' : ''}`}>
      <div
        className={`uploader__zone${dragging ? ' uploader__zone--over' : ''}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <button
          type="button"
          className="uploader__trigger"
          onClick={() => inputRef.current?.click()}
          disabled={busy}
        >
          <span className="uploader__glyph" aria-hidden="true">
            {busy ? <Spinner label="Uploading" size={20} /> : <UploadIcon size={20} />}
          </span>
          <span className="uploader__copy">
            <span className="uploader__headline">
              {busy ? 'Uploading…' : 'Add a document'}
            </span>
            <span className="uploader__meta">
              PDF, .txt or .md · up to {formatBytes(MAX_UPLOAD_BYTES)}
              <span className="uploader__drag-hint"> · or drop it here</span>
            </span>
          </span>
        </button>

        <input
          ref={inputRef}
          className="visually-hidden"
          type="file"
          multiple
          accept={UPLOAD_ACCEPT_ATTR}
          onChange={handleInput}
          aria-label="Choose documents to upload"
        />
      </div>

      {rejections.length > 0 ? (
        <ul className="uploader__rejections" role="alert">
          {rejections.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
