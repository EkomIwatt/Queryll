/**
 * Test harness.
 *
 * The mock is a `QueryllApi` — the API-client boundary — not a patched `fetch`. That is the whole
 * point: every contract shape is checked by the compiler on the way into this file, so a change to
 * `src/api/types.ts` (the hand-written copy of the frozen contracts) breaks these fixtures loudly
 * instead of leaving them silently describing an API that no longer exists.
 */

import { render, type RenderOptions } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import type { ReactElement, ReactNode } from 'react';
import { ApiProvider } from '../src/api/ApiProvider';
import { AuthProvider } from '../src/auth/AuthContext';
import type { QueryllApi } from '../src/api/client';
import type {
  AskEvent,
  AuthSession,
  Chunk,
  ChunkDetailResponse,
  ChunkSummary,
  Citation,
  Conversation,
  Document,
  Message,
  User,
} from '../src/api/types';

/* ------------------------------------------------------------------ *
 * Fixtures — all shaped by the contract types
 * ------------------------------------------------------------------ */

export const USER: User = {
  id: 'user-1',
  email: 'reader@example.com',
  display_name: 'reader',
  created_at: '2026-09-01T09:00:00Z',
};

export const SESSION: AuthSession = { access_token: 'access-token-1', user: USER };

export function makeDocument(overrides: Partial<Document> = {}): Document {
  return {
    id: 'doc-1',
    filename: 'sampling-methods.pdf',
    mime_type: 'application/pdf',
    size_bytes: 482_112,
    status: 'ready',
    progress: 1,
    page_count: 24,
    chunk_count: 61,
    error_message: null,
    created_at: '2026-09-05T11:00:00Z',
    indexed_at: '2026-09-05T11:00:24Z',
    ...overrides,
  };
}

export function makeCitation(overrides: Partial<Citation> = {}): Citation {
  return {
    index: 1,
    chunk_id: 'chunk-a',
    document_id: 'doc-1',
    filename: 'sampling-methods.pdf',
    page_start: 12,
    page_end: 12,
    heading_path: '3. Methods > 3.2 Sampling',
    similarity: 0.812,
    snippet: 'Stratified sampling divides the population into strata before drawing from each.',
    ...overrides,
  };
}

export function makeChunk(overrides: Partial<Chunk> = {}): Chunk {
  return {
    id: 'chunk-a',
    ordinal: 17,
    page_start: 12,
    page_end: 12,
    heading_path: '3. Methods > 3.2 Sampling',
    preview: 'Stratified sampling divides the population into strata',
    text: 'Stratified sampling divides the population into strata before drawing from each, which reduces variance when the strata are internally homogeneous.',
    token_count: 38,
    ...overrides,
  };
}

export function makeChunkSummary(overrides: Partial<ChunkSummary> = {}): ChunkSummary {
  const { text: _text, token_count: _tokens, ...summary } = makeChunk();
  return { ...summary, ...overrides };
}

export function makeConversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: 'conv-1',
    title: 'How is the sample stratified?',
    created_at: '2026-09-06T10:00:00Z',
    ...overrides,
  };
}

export function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-1',
    role: 'assistant',
    content: 'The sample is stratified by region [1].',
    citations: [makeCitation()],
    created_at: '2026-09-06T10:00:05Z',
    ...overrides,
  };
}

/** Contract 7 §3 — a complete, well-ordered event script. */
export function askScript(options: {
  sources?: Citation[];
  tokens?: string[];
  citationsUsed?: number[];
  insufficientContext?: boolean;
  messageId?: string;
} = {}): AskEvent[] {
  const sources = options.sources ?? [makeCitation()];
  return [
    {
      event: 'retrieval',
      data: { sources, insufficient_context: options.insufficientContext ?? false },
    },
    ...(options.tokens ?? ['The sample is stratified by region ', '[1]', '.']).map(
      (text): AskEvent => ({ event: 'token', data: { text } }),
    ),
    {
      event: 'done',
      data: {
        message_id: options.messageId ?? 'msg-new',
        citations_used: options.citationsUsed ?? [1],
      },
    },
  ];
}

/* ------------------------------------------------------------------ *
 * The mock API
 * ------------------------------------------------------------------ */

export type MockApi = {
  [K in keyof QueryllApi]: ReturnType<typeof vi.fn>;
} & QueryllApi;

export interface MockApiOptions {
  documents?: Document[];
  conversations?: Conversation[];
  messages?: Message[];
  chunkDetail?: ChunkDetailResponse;
  chunks?: ChunkSummary[];
  session?: AuthSession | null;
  ask?: QueryllApi['ask'];
}

export function createMockApi(options: MockApiOptions = {}): MockApi {
  const documents = options.documents ?? [];
  const emptyAsk: QueryllApi['ask'] = async function* () {
    /* nothing streams unless a test says so */
  };

  return {
    signup: vi.fn(async () => options.session ?? SESSION),
    login: vi.fn(async () => options.session ?? SESSION),
    refresh: vi.fn(async () => (options.session === undefined ? SESSION : options.session)),
    logout: vi.fn(async () => undefined),
    me: vi.fn(async () => USER),

    uploadDocument: vi.fn(async (file: File) =>
      makeDocument({
        id: `doc-${file.name}`,
        filename: file.name,
        mime_type: file.type,
        size_bytes: file.size,
        status: 'pending',
        progress: 0,
        page_count: null,
        chunk_count: null,
        indexed_at: null,
      }),
    ),
    listDocuments: vi.fn(async () => ({ documents, next_cursor: null })),
    getDocument: vi.fn(async (id: string) => documents.find((d) => d.id === id) ?? makeDocument()),
    listChunks: vi.fn(async () => ({ chunks: options.chunks ?? [], next_cursor: null })),
    getChunk: vi.fn(
      async () => options.chunkDetail ?? { chunk: makeChunk(), prev: null, next: null },
    ),
    reindexDocument: vi.fn(async (id: string) =>
      makeDocument({ id, status: 'pending', progress: 0, error_message: null, indexed_at: null }),
    ),
    deleteDocument: vi.fn(async () => undefined),

    createConversation: vi.fn(async () => makeConversation({ id: 'conv-new', title: null })),
    listConversations: vi.fn(async () => ({ conversations: options.conversations ?? [] })),
    getConversation: vi.fn(async (id: string) => ({
      conversation: makeConversation({ id }),
      messages: options.messages ?? [],
    })),
    deleteConversation: vi.fn(async () => undefined),

    ask: vi.fn(options.ask ?? emptyAsk),
  } as MockApi;
}

/* ------------------------------------------------------------------ *
 * Rendering
 * ------------------------------------------------------------------ */

export interface RenderAppOptions extends Omit<RenderOptions, 'wrapper'> {
  api: QueryllApi;
  route?: string;
  /** Skip the auth shell — for components that do not need a session. */
  withAuth?: boolean;
}

export function renderWithProviders(ui: ReactElement, options: RenderAppOptions) {
  const { api, route = '/', withAuth = true, ...rest } = options;

  function Wrapper({ children }: { children: ReactNode }) {
    const tree = <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>;
    return (
      <ApiProvider api={api}>
        {withAuth ? <AuthProvider>{tree}</AuthProvider> : tree}
      </ApiProvider>
    );
  }

  return render(ui, { wrapper: Wrapper, ...rest });
}
