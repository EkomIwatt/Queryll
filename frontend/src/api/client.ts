/**
 * The Queryll API client.
 *
 * This interface is the mock boundary for the entire frontend test suite. Tests substitute a
 * `QueryllApi` — never a patched `fetch` — so the contract shapes live in exactly one typed
 * place (`types.ts`) and a contract change breaks compilation rather than a string comparison
 * three layers down.
 *
 * Auth rules it enforces on every call (Contract 1 / Contract 9):
 *   · the access token lives in memory only, never `localStorage`;
 *   · a 401 triggers exactly one refresh and one retry, then the session is declared over;
 *   · concurrent 401s share a single in-flight refresh rather than racing each other;
 *   · the client never sends a user id — ownership is derived from the token, always.
 */

import { ApiError, NetworkError } from './errors';
import { createAskStream, type AskStreamFn } from './askStream';
import type {
  AuthSession,
  ChunkDetailResponse,
  ChunkListResponse,
  Conversation,
  ConversationDetailResponse,
  ConversationListResponse,
  Credentials,
  Document,
  DocumentListResponse,
  User,
} from './types';
import { isErrorEnvelope } from './types';

export interface PageParams {
  limit?: number;
  cursor?: string | null;
}

export interface QueryllApi {
  /* Contract 1 — authentication */
  signup(credentials: Credentials): Promise<AuthSession>;
  login(credentials: Credentials): Promise<AuthSession>;
  /** Uses the httpOnly refresh cookie. Resolves null when there is no live session. */
  refresh(): Promise<AuthSession | null>;
  logout(): Promise<void>;
  me(): Promise<User>;

  /* Contract 6 — documents */
  uploadDocument(file: File, signal?: AbortSignal): Promise<Document>;
  listDocuments(params?: PageParams): Promise<DocumentListResponse>;
  getDocument(id: string): Promise<Document>;
  listChunks(documentId: string, params?: PageParams): Promise<ChunkListResponse>;
  getChunk(documentId: string, chunkId: string): Promise<ChunkDetailResponse>;
  reindexDocument(id: string): Promise<Document>;
  deleteDocument(id: string): Promise<void>;

  /* Contract 7 §9 — conversations */
  createConversation(title?: string | null): Promise<Conversation>;
  listConversations(): Promise<ConversationListResponse>;
  getConversation(id: string): Promise<ConversationDetailResponse>;
  deleteConversation(id: string): Promise<void>;

  /* Contract 7 §1-§3 — the streamed answer */
  ask: AskStreamFn;
}

/** The in-memory access-token holder. Never persisted; a reload re-derives it from the cookie. */
export interface TokenStore {
  get(): string | null;
  set(token: string | null): void;
}

export function createTokenStore(): TokenStore {
  let token: string | null = null;
  return {
    get: () => token,
    set: (next) => {
      token = next;
    },
  };
}

export interface HttpApiOptions {
  baseUrl: string;
  tokens: TokenStore;
  fetchImpl?: typeof fetch;
  /** Fired when a refresh attempt fails — the session is over and the app should route to login. */
  onSessionEnded?: () => void;
  /** Fired whenever a refresh succeeds, so the auth shell can pick up the new user + token. */
  onSessionRefreshed?: (session: AuthSession) => void;
}

export function createHttpApi(options: HttpApiOptions): QueryllApi {
  const baseUrl = options.baseUrl.replace(/\/+$/, '');
  const doFetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  const { tokens } = options;

  /** Single-flight refresh: N concurrent 401s wait on one request, not N. */
  let inFlightRefresh: Promise<AuthSession | null> | null = null;

  async function rawFetch(path: string, init: RequestInit): Promise<Response> {
    try {
      return await doFetch(`${baseUrl}${path}`, { credentials: 'include', ...init });
    } catch (cause) {
      if (init.signal?.aborted) throw cause;
      throw new NetworkError();
    }
  }

  async function refreshOnce(): Promise<AuthSession | null> {
    inFlightRefresh ??= (async () => {
      try {
        const response = await rawFetch('/api/auth/refresh', { method: 'POST' });
        if (!response.ok) return null;
        const session = (await response.json()) as AuthSession;
        tokens.set(session.access_token);
        options.onSessionRefreshed?.(session);
        return session;
      } catch {
        return null;
      } finally {
        // Cleared on the microtask after every waiter has resolved.
        queueMicrotask(() => {
          inFlightRefresh = null;
        });
      }
    })();
    return inFlightRefresh;
  }

  interface RequestOptions {
    method?: string;
    body?: BodyInit;
    json?: unknown;
    signal?: AbortSignal;
    /** Skip the refresh-and-retry dance — used by the auth routes themselves. */
    anonymous?: boolean;
  }

  async function send(path: string, opts: RequestOptions = {}): Promise<Response> {
    const build = (token: string | null): RequestInit => {
      const headers = new Headers();
      if (opts.json !== undefined) headers.set('Content-Type', 'application/json');
      if (token) headers.set('Authorization', `Bearer ${token}`);
      const body = opts.json !== undefined ? JSON.stringify(opts.json) : opts.body;
      return {
        method: opts.method ?? 'GET',
        headers,
        ...(body !== undefined ? { body } : {}),
        ...(opts.signal ? { signal: opts.signal } : {}),
      };
    };

    let response = await rawFetch(path, build(tokens.get()));

    if (response.status === 401 && !opts.anonymous) {
      const session = await refreshOnce();
      if (session) {
        response = await rawFetch(path, build(session.access_token));
      } else {
        tokens.set(null);
        options.onSessionEnded?.();
      }
    }

    if (!response.ok) throw await toApiError(response);
    return response;
  }

  async function json<T>(path: string, opts?: RequestOptions): Promise<T> {
    const response = await send(path, opts);
    return (await response.json()) as T;
  }

  async function empty(path: string, opts?: RequestOptions): Promise<void> {
    await send(path, opts);
  }

  const rememberSession = (session: AuthSession): AuthSession => {
    tokens.set(session.access_token);
    return session;
  };

  return {
    /* ---- Contract 1 ---- */
    async signup(credentials) {
      return rememberSession(
        await json<AuthSession>('/api/auth/signup', {
          method: 'POST',
          json: credentials,
          anonymous: true,
        }),
      );
    },

    async login(credentials) {
      return rememberSession(
        await json<AuthSession>('/api/auth/login', {
          method: 'POST',
          json: credentials,
          anonymous: true,
        }),
      );
    },

    refresh: refreshOnce,

    async logout() {
      try {
        await empty('/api/auth/logout', { method: 'POST', anonymous: true });
      } finally {
        tokens.set(null);
      }
    },

    me() {
      return json<User>('/api/auth/me');
    },

    /* ---- Contract 6 ---- */
    uploadDocument(file, signal) {
      const form = new FormData();
      form.append('file', file);
      return json<Document>('/api/documents', {
        method: 'POST',
        body: form,
        ...(signal ? { signal } : {}),
      });
    },

    listDocuments(params) {
      return json<DocumentListResponse>(`/api/documents${pageQuery(params)}`);
    },

    getDocument(id) {
      return json<Document>(`/api/documents/${encodeURIComponent(id)}`);
    },

    listChunks(documentId, params) {
      return json<ChunkListResponse>(
        `/api/documents/${encodeURIComponent(documentId)}/chunks${pageQuery(params)}`,
      );
    },

    getChunk(documentId, chunkId) {
      return json<ChunkDetailResponse>(
        `/api/documents/${encodeURIComponent(documentId)}/chunks/${encodeURIComponent(chunkId)}`,
      );
    },

    reindexDocument(id) {
      return json<Document>(`/api/documents/${encodeURIComponent(id)}/reindex`, { method: 'POST' });
    },

    deleteDocument(id) {
      return empty(`/api/documents/${encodeURIComponent(id)}`, { method: 'DELETE' });
    },

    /* ---- Contract 7 §9 ---- */
    createConversation(title = null) {
      return json<Conversation>('/api/conversations', { method: 'POST', json: { title } });
    },

    listConversations() {
      return json<ConversationListResponse>('/api/conversations');
    },

    getConversation(id) {
      return json<ConversationDetailResponse>(`/api/conversations/${encodeURIComponent(id)}`);
    },

    deleteConversation(id) {
      return empty(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
    },

    /* ---- Contract 7 §1-§3 ---- */
    ask: createAskStream({
      baseUrl,
      getAccessToken: async () => tokens.get(),
      refreshAccessToken: async () => {
        const session = await refreshOnce();
        if (!session) {
          tokens.set(null);
          options.onSessionEnded?.();
        }
        return session?.access_token ?? null;
      },
      ...(options.fetchImpl ? { fetchImpl: options.fetchImpl } : {}),
    }),
  };
}

function pageQuery(params?: PageParams): string {
  const search = new URLSearchParams();
  if (params?.limit !== undefined) search.set('limit', String(params.limit));
  if (params?.cursor) search.set('cursor', params.cursor);
  const query = search.toString();
  return query ? `?${query}` : '';
}

async function toApiError(response: Response): Promise<ApiError> {
  let sentence = fallbackSentence(response.status);
  try {
    const payload: unknown = await response.json();
    if (isErrorEnvelope(payload) && payload.error.trim()) sentence = payload.error;
  } catch {
    // A non-JSON body (a proxy's own 502 page, say) keeps the fallback sentence.
  }
  return new ApiError(sentence, response.status);
}

/**
 * Used only when the server did not send an envelope it was supposed to. Every one of these is a
 * complete sentence, because Contract 9 promises the UI can render `error` directly.
 */
function fallbackSentence(status: number): string {
  switch (status) {
    case 401:
      return 'Your session has expired. Please sign in again.';
    case 404:
      return 'That was not found.';
    case 409:
      return 'That document is being processed right now. Try again once it finishes.';
    case 413:
      return 'That file is too large. The limit is 20 MB.';
    case 415:
      return 'That file type is not supported. Upload a PDF, .txt or .md file.';
    case 422:
      return 'Some of the details were not valid. Check them and try again.';
    case 503:
      return 'Search is temporarily unavailable. Try again in a moment.';
    default:
      return 'Something went wrong on the server. Please try again.';
  }
}
