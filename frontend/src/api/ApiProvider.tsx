/**
 * Dependency injection for the API client.
 *
 * The whole app reads its `QueryllApi` from here, which is what makes "mock the HTTP layer at the
 * API-client boundary, not at `fetch`" true in practice rather than aspirationally: a test renders
 * the real component tree inside an `ApiProvider` holding a hand-written fake, and the contract
 * shapes are checked by the compiler on the way in.
 */

import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { API_BASE_URL } from '../config';
import { createHttpApi, createTokenStore, type QueryllApi, type TokenStore } from './client';

interface ApiContextValue {
  api: QueryllApi;
  tokens: TokenStore;
}

const ApiContext = createContext<ApiContextValue | null>(null);

export interface ApiProviderProps {
  children: ReactNode;
  /** Supplied by tests and by the dev-mode mock backend. Omitted in production. */
  api?: QueryllApi;
  tokens?: TokenStore;
  onSessionEnded?: () => void;
}

export function ApiProvider({ children, api, tokens, onSessionEnded }: ApiProviderProps) {
  const value = useMemo<ApiContextValue>(() => {
    const store = tokens ?? createTokenStore();
    return {
      tokens: store,
      api:
        api ??
        createHttpApi({
          baseUrl: API_BASE_URL,
          tokens: store,
          ...(onSessionEnded ? { onSessionEnded } : {}),
        }),
    };
    // The client is created once per provider; swapping it mid-session is never wanted.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, tokens]);

  return <ApiContext.Provider value={value}>{children}</ApiContext.Provider>;
}

export function useApi(): QueryllApi {
  const value = useContext(ApiContext);
  if (!value) throw new Error('useApi must be used inside an <ApiProvider>');
  return value.api;
}

export function useTokenStore(): TokenStore {
  const value = useContext(ApiContext);
  if (!value) throw new Error('useTokenStore must be used inside an <ApiProvider>');
  return value.tokens;
}
