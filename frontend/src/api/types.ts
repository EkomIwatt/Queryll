/**
 * Contract types for the Queryll API.
 *
 * Hand-written from the frozen INTERFACE CONTRACTS block in the repo-root CLAUDE.md
 * (ratified 2026-09-07). Not generated. Instance 3 has never seen Instance 2's code, so
 * this file is the single typed place where the contract lives on the browser side —
 * every mock, every test fixture and every component reads its shapes from here.
 *
 * Section references below point at the contract paragraph each shape comes from.
 */

/** ISO-8601 UTC with a trailing `Z` (project-wide convention). */
export type Iso8601 = string;

/* ------------------------------------------------------------------ *
 * Contract 1 — Authentication
 * ------------------------------------------------------------------ */

export interface User {
  id: string;
  email: string;
  display_name: string;
  created_at: Iso8601;
}

/** Shape returned by signup / login / refresh. */
export interface AuthSession {
  access_token: string;
  user: User;
}

export interface Credentials {
  email: string;
  password: string;
}

/** Contract 1: "Passwords: argon2 via passlib. Minimum 8 characters." */
export const PASSWORD_MIN_LENGTH = 8;

/* ------------------------------------------------------------------ *
 * Contract 6 — Documents HTTP API
 * ------------------------------------------------------------------ */

/** Contract 2 / Contract 3 §4 — the `document_status` enum. */
export const DOCUMENT_STATUSES = ['pending', 'processing', 'ready', 'failed'] as const;
export type DocumentStatus = (typeof DOCUMENT_STATUSES)[number];

export interface Document {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  status: DocumentStatus;
  /** 0.0 .. 1.0 — Contract 3 §7. */
  progress: number;
  page_count: number | null;
  chunk_count: number | null;
  /** Non-null iff `status === 'failed'`. Rendered verbatim; never re-worded by the UI. */
  error_message: string | null;
  created_at: Iso8601;
  indexed_at: Iso8601 | null;
}

/** Contract 6 §2 — `GET /api/documents`. */
export interface DocumentListResponse {
  documents: Document[];
  next_cursor: string | null;
}

/** Contract 6 §3. `preview` is the first 200 chars of the chunk text. */
export interface ChunkSummary {
  id: string;
  ordinal: number;
  page_start: number | null;
  page_end: number | null;
  heading_path: string | null;
  preview: string;
}

export interface Chunk extends ChunkSummary {
  text: string;
  token_count: number;
}

export interface ChunkListResponse {
  chunks: ChunkSummary[];
  next_cursor: string | null;
}

/** Contract 6 §3 — the single-chunk route returns immediate neighbours for context. */
export interface ChunkDetailResponse {
  chunk: Chunk;
  prev: Chunk | null;
  next: Chunk | null;
}

/** Contract 5 §1 / Contract 6 §1 — accepted upload types and the size cap. */
export const ACCEPTED_MIME_TYPES = ['application/pdf', 'text/plain', 'text/markdown'] as const;
export type AcceptedMimeType = (typeof ACCEPTED_MIME_TYPES)[number];

/** Extensions matching ACCEPTED_MIME_TYPES; browsers report `.md` inconsistently. */
export const ACCEPTED_EXTENSIONS = ['.pdf', '.txt', '.md', '.markdown'] as const;

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

/* ------------------------------------------------------------------ *
 * Contract 8 — The Citation object
 * ------------------------------------------------------------------ */

export interface Citation {
  /** 1-based; the `[n]` in the answer text. Meaningful only within its own message (§1). */
  index: number;
  /** The stable handle. Passages are resolved by this — never by index or page (§2). */
  chunk_id: string;
  document_id: string;
  filename: string;
  page_start: number | null;
  page_end: number | null;
  heading_path: string | null;
  /** 0..1, rounded to 3dp. Display and debugging only. */
  similarity: number;
  /** <= 300 chars, for the inline hover card. Never the source of truth for the passage (§2). */
  snippet: string;
}

/* ------------------------------------------------------------------ *
 * Contract 7 — Conversations, and the streamed answer
 * ------------------------------------------------------------------ */

export interface Conversation {
  id: string;
  title: string | null;
  created_at: Iso8601;
}

export type MessageRole = 'user' | 'assistant';

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  citations: Citation[];
  created_at: Iso8601;
}

export interface ConversationListResponse {
  conversations: Conversation[];
}

export interface ConversationDetailResponse {
  conversation: Conversation;
  messages: Message[];
}

/** Contract 7 §1 — `document_ids: null` means "search all of the user's documents". */
export interface AskRequest {
  question: string;
  document_ids: string[] | null;
}

/* --- Contract 7 §3: the SSE event sequence, as a discriminated union --- */

/** ALWAYS FIRST, exactly once — sent before the model is called. */
export interface RetrievalEventData {
  sources: Citation[];
  insufficient_context: boolean;
}

/** Zero or more, in order. */
export interface TokenEventData {
  text: string;
}

/** Exactly once, terminal. `citations_used` holds 1-based indices into `sources`. */
export interface DoneEventData {
  message_id: string;
  citations_used: number[];
}

/** Terminal, replaces `done`. */
export interface StreamErrorEventData {
  error: string;
  code: string;
}

export type AskEvent =
  | { event: 'retrieval'; data: RetrievalEventData }
  | { event: 'token'; data: TokenEventData }
  | { event: 'done'; data: DoneEventData }
  | { event: 'error'; data: StreamErrorEventData };

export type AskEventName = AskEvent['event'];

/**
 * Contract 7 §5 — the fixed sentence the server sends as the sole `token` event when
 * nothing clears the similarity floor. Duplicated here only so the UI can recognise the
 * grounding-refusal path for styling; the text always comes from the stream, never from here.
 */
export const INSUFFICIENT_CONTEXT_SENTENCE =
  'I could not find anything about that in your documents.';

/* ------------------------------------------------------------------ *
 * Contract 9 — Errors
 * ------------------------------------------------------------------ */

/** Every non-2xx response, from every endpoint. One human-readable sentence. */
export interface ErrorEnvelope {
  error: string;
}

export function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { error?: unknown }).error === 'string'
  );
}
