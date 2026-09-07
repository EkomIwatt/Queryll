/**
 * Where a half-typed question waits.
 *
 * A 401 mid-session sends the reader to the login screen. Losing what they had typed on the way
 * is a small betrayal that is entirely avoidable, so the composer's text lives here — module
 * scope, in memory, keyed by conversation — and survives every route change inside the session.
 *
 * Not `sessionStorage`: a question can quote the document it is about, and there is no reason for
 * that to outlive the tab or touch disk.
 */

const drafts = new Map<string, string>();

/** The key used for a question typed before any conversation exists yet. */
export const NEW_CONVERSATION = 'new';

export const draftStore = {
  get(key: string): string {
    return drafts.get(key) ?? '';
  },
  set(key: string, value: string): void {
    if (value) drafts.set(key, value);
    else drafts.delete(key);
  },
  clear(key: string): void {
    drafts.delete(key);
  },
  /** Move the draft written before a conversation existed onto the conversation that was created. */
  rekey(from: string, to: string): void {
    const value = drafts.get(from);
    if (value === undefined) return;
    drafts.delete(from);
    drafts.set(to, value);
  },
  reset(): void {
    drafts.clear();
  },
};
