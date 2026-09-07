/** Runtime configuration. The API base URL is the only thing the browser needs to be told. */

const raw = import.meta.env.VITE_API_BASE_URL;

/**
 * Falls back to a same-origin relative base so a preview deploy behind a rewrite still works,
 * and so tests never depend on an env file being present.
 */
export const API_BASE_URL: string = (typeof raw === 'string' && raw.trim() ? raw.trim() : '').replace(
  /\/+$/,
  '',
);
