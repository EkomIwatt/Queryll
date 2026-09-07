import { useCallback, useEffect, useState } from 'react';
import { MoonIcon, SunIcon } from './icons';

type Theme = 'light' | 'dark' | 'system';
const STORAGE_KEY = 'queryll.theme';

/**
 * Theme preference — and only the theme preference — is allowed in `localStorage`. It is a display
 * setting, it is worthless to an attacker, and the alternative is a flash of the wrong theme on
 * every page load. The access token is a different matter and never goes near web storage.
 */
function readStored(): Theme {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === 'light' || raw === 'dark' || raw === 'system') return raw;
  } catch {
    // Private mode, or storage disabled. System preference it is.
  }
  return 'system';
}

function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
}

/** Call once at startup so the attribute is set before first paint of the app tree. */
export function initTheme(): void {
  applyTheme(readStored());
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(readStored);

  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Nothing to do; the in-memory choice still applies for this session.
    }
  }, [theme]);

  const prefersDark =
    typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches;
  const isDark = theme === 'dark' || (theme === 'system' && prefersDark);

  const toggle = useCallback(() => setTheme(isDark ? 'light' : 'dark'), [isDark]);

  return (
    <button
      type="button"
      className="btn btn--ghost theme-toggle"
      onClick={toggle}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      aria-pressed={isDark}
    >
      {isDark ? <SunIcon size={17} /> : <MoonIcon size={17} />}
    </button>
  );
}
