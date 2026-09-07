import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import type { Document } from '../api/types';
import { ArrowUpIcon } from '../ui/icons';
import { Spinner } from '../ui/primitives';

export interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  /** `documentIds` is null when every document should be searched — Contract 7 §1. */
  onSubmit: (question: string, documentIds: string[] | null) => void;
  readyDocuments: readonly Document[];
  busy: boolean;
  onCancel: () => void;
  disabled?: boolean;
}

export function Composer({
  value,
  onChange,
  onSubmit,
  readyDocuments,
  busy,
  onCancel,
  disabled = false,
}: ComposerProps) {
  const [scope, setScope] = useState<Set<string>>(new Set());
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Drop any selected document that has since been deleted or re-indexed out of `ready`.
  useEffect(() => {
    setScope((current) => {
      const alive = new Set(readyDocuments.map((d) => d.id));
      const next = new Set([...current].filter((id) => alive.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [readyDocuments]);

  // Grow the field with the question rather than making a long one scroll in a two-line box.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`;
  }, [value]);

  const trimmed = value.trim();
  const canSend = trimmed.length > 0 && !busy && !disabled;
  const scopedIds = scope.size === 0 ? null : [...scope];

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!canSend) return;
    onSubmit(trimmed, scopedIds);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift+Enter breaks the line — the convention every reader already has.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  const scopeLabel =
    scope.size === 0
      ? `All ${readyDocuments.length} document${readyDocuments.length === 1 ? '' : 's'}`
      : `${scope.size} of ${readyDocuments.length} document${readyDocuments.length === 1 ? '' : 's'}`;

  return (
    <form className="composer" onSubmit={submit}>
      <div className="composer__field">
        <label className="visually-hidden" htmlFor="composer-question">
          Your question
        </label>
        <textarea
          id="composer-question"
          ref={textareaRef}
          className="composer__input"
          rows={1}
          placeholder="Ask something answerable from your documents…"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={handleKeyDown}
        />

        {busy ? (
          <button
            type="button"
            className="btn btn--secondary composer__send"
            onClick={onCancel}
            aria-label="Stop this answer"
          >
            <Spinner label="Answering" />
            Stop
          </button>
        ) : (
          <button
            type="submit"
            className="btn btn--primary composer__send"
            disabled={!canSend}
            aria-label="Ask"
          >
            <ArrowUpIcon size={17} />
          </button>
        )}
      </div>

      {readyDocuments.length > 0 ? (
        <details className="scope">
          <summary className="scope__summary">
            <span className="eyebrow">Searching</span>
            <span className="scope__value">{scopeLabel}</span>
          </summary>
          <fieldset className="scope__panel">
            <legend className="visually-hidden">Limit the search to particular documents</legend>
            <p className="scope__hint">
              Leave everything unticked to search your whole library.
            </p>
            <ul className="scope__list">
              {readyDocuments.map((document) => (
                <li key={document.id}>
                  <label className="scope__item">
                    <input
                      type="checkbox"
                      checked={scope.has(document.id)}
                      onChange={(e) => {
                        setScope((current) => {
                          const next = new Set(current);
                          if (e.target.checked) next.add(document.id);
                          else next.delete(document.id);
                          return next;
                        });
                      }}
                    />
                    <span>{document.filename}</span>
                  </label>
                </li>
              ))}
            </ul>
          </fieldset>
        </details>
      ) : null}
    </form>
  );
}
