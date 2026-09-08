/**
 * The auth shell — Contract 1.
 *
 * The access token lives in `TokenStore`, which is a closure variable and nothing else. It is
 * never written to `localStorage` or `sessionStorage`: a token in web storage is readable by any
 * script that gets a foothold on the page, and the httpOnly refresh cookie already gives us
 * durability across reloads for free.
 *
 * So a page load starts in `restoring`, spends one request on `POST /api/auth/refresh`, and lands
 * in either `authenticated` or `anonymous`. Nothing else in the app renders until it does.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useApi } from '../api/ApiProvider';
import type { AuthSession, Credentials, User } from '../api/types';

export type AuthStatus = 'restoring' | 'authenticated' | 'anonymous';

export interface AuthContextValue {
  status: AuthStatus;
  user: User | null;
  login(credentials: Credentials): Promise<void>;
  signup(credentials: Credentials): Promise<void>;
  logout(): Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export interface AuthProviderProps {
  children: ReactNode;
  /**
   * Bumped by `App` when the API client's single refresh attempt fails. That is the moment the
   * session is genuinely over, and it is the only thing that can flip an authenticated shell back
   * to anonymous without an explicit sign-out.
   */
  sessionEndedAt?: number;
}

export function AuthProvider({ children, sessionEndedAt = 0 }: AuthProviderProps) {
  const api = useApi();
  const [status, setStatus] = useState<AuthStatus>('restoring');
  const [user, setUser] = useState<User | null>(null);
  const restored = useRef(false);

  useEffect(() => {
    // React 18+ StrictMode double-invokes effects in dev; one restore attempt is enough.
    if (restored.current) return;
    restored.current = true;

    let cancelled = false;
    void (async () => {
      const session = await api.refresh().catch(() => null);
      if (cancelled) return;
      if (session) {
        setUser(session.user);
        setStatus('authenticated');
      } else {
        setUser(null);
        setStatus('anonymous');
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [api]);

  useEffect(() => {
    if (sessionEndedAt === 0) return;
    setUser(null);
    setStatus('anonymous');
  }, [sessionEndedAt]);

  const adopt = useCallback((session: AuthSession) => {
    setUser(session.user);
    setStatus('authenticated');
  }, []);

  const login = useCallback(
    async (credentials: Credentials) => {
      adopt(await api.login(credentials));
    },
    [api, adopt],
  );

  const signup = useCallback(
    async (credentials: Credentials) => {
      adopt(await api.signup(credentials));
    },
    [api, adopt],
  );

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setUser(null);
      setStatus('anonymous');
    }
  }, [api]);

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, login, signup, logout }),
    [status, user, login, signup, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside an <AuthProvider>');
  return value;
}
