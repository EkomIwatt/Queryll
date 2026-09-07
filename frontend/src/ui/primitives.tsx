import { useEffect, useRef, type ReactNode } from 'react';
import { AlertIcon, CloseIcon } from './icons';

/* ------------------------------------------------------------------ *
 * Spinner
 * ------------------------------------------------------------------ */

export function Spinner({ label = 'Loading', size = 16 }: { label?: string; size?: number }) {
  return (
    <span className="spinner" style={{ width: size, height: size }} role="status">
      <span className="visually-hidden">{label}</span>
    </span>
  );
}

/* ------------------------------------------------------------------ *
 * Progress
 * ------------------------------------------------------------------ */

export interface ProgressProps {
  /** 0..1. Ignored when `indeterminate`. */
  value: number;
  /**
   * Contract 3 §7: parsing and chunking happen before any chunk count is known, so `processing`
   * at `progress === 0.0` genuinely has no fraction to show. It gets a moving bar, not a bar
   * pinned at zero — those look identical to a hang.
   */
  indeterminate?: boolean;
  label: string;
}

export function Progress({ value, indeterminate = false, label }: ProgressProps) {
  const pct = Math.round(Math.min(1, Math.max(0, value)) * 100);
  return (
    <div
      className={`progress${indeterminate ? ' progress--indeterminate' : ''}`}
      role="progressbar"
      aria-label={label}
      {...(indeterminate
        ? {}
        : { 'aria-valuenow': pct, 'aria-valuemin': 0, 'aria-valuemax': 100 })}
    >
      <div className="progress__fill" style={indeterminate ? undefined : { width: `${pct}%` }} />
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Notice — renders a server sentence verbatim
 * ------------------------------------------------------------------ */

export function ErrorNotice({ children }: { children: ReactNode }) {
  return (
    <div className="notice notice--error" role="alert">
      <AlertIcon className="notice__icon" size={16} />
      <span>{children}</span>
    </div>
  );
}

export function InfoNotice({ children }: { children: ReactNode }) {
  return (
    <div className="notice notice--info">
      <span>{children}</span>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Empty state
 * ------------------------------------------------------------------ */

export interface EmptyStateProps {
  eyebrow?: string;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}

export function EmptyState({ eyebrow, title, children, action }: EmptyStateProps) {
  return (
    <div className="empty">
      {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
      <h2 className="empty__title">{title}</h2>
      {children ? <div className="empty__body">{children}</div> : null}
      {action ? <div className="empty__action">{action}</div> : null}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Modal — native <dialog>, so Escape, focus trapping and inertness are the platform's job
 * ------------------------------------------------------------------ */

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  /** Rendered next to the title — used for the passage viewer's prev/next controls. */
  toolbar?: ReactNode;
  children: ReactNode;
  labelledBy?: string;
  className?: string;
}

export function Modal({ open, onClose, title, toolbar, children, className }: ModalProps) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    // Fires for Escape and for programmatic close alike.
    const handleClose = () => onClose();
    dialog.addEventListener('close', handleClose);
    return () => dialog.removeEventListener('close', handleClose);
  }, [onClose]);

  return (
    <dialog
      ref={ref}
      className={`modal${className ? ` ${className}` : ''}`}
      aria-label={title}
      onClick={(event) => {
        // Clicking the backdrop — the dialog element itself — dismisses.
        if (event.target === ref.current) onClose();
      }}
    >
      <div className="modal__frame">
        <header className="modal__header">
          <div className="modal__heading">{toolbar}</div>
          <button
            type="button"
            className="btn btn--ghost modal__close"
            onClick={onClose}
            aria-label="Close"
          >
            <CloseIcon size={18} />
          </button>
        </header>
        <div className="modal__body">{children}</div>
      </div>
    </dialog>
  );
}
