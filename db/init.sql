-- Queryll — database schema
--
-- FROZEN ARTIFACT. This file is Contract 2 in CLAUDE.md, ratified 2026-09-07 and committed to
-- `main` before any worktree was created. It is owned by NO instance.
--
-- Instance 1 (worker) and Instance 2 (api) each declare SQLAlchemy models for ONLY the tables
-- they touch, mapping this DDL exactly. There is no Alembic in this project and no `create_all`
-- against a real database: this file is the single source of truth for the schema.
--
-- A schema change is an ESCALATION, not an edit. If you believe something here is wrong, write a
-- proposed amendment in the ESCALATIONS section of CLAUDE.md and wait for the human.
--
-- Column ownership (who may WRITE what) is Contract 3 §1. Read it before writing any UPDATE.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  display_name  text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- documents
--   Written by Instance 2 on insert (id .. created_at, content).
--   Lifecycle columns (status, progress, page_count, chunk_count, error_message,
--   indexed_at) are written by Instance 1, with the single re-index exception in
--   Contract 3 §1.
--   `content` holds the original file bytes: the API and the worker share nothing
--   but this database, so there is deliberately no disk and no object store.
-- ---------------------------------------------------------------------------

CREATE TYPE document_status AS ENUM ('pending', 'processing', 'ready', 'failed');

CREATE TABLE documents (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  filename      text NOT NULL,
  mime_type     text NOT NULL,
  size_bytes    integer NOT NULL,
  content       bytea NOT NULL,              -- the original file; never leaves the database
  status        document_status NOT NULL DEFAULT 'pending',
  progress      real NOT NULL DEFAULT 0.0,   -- 0.0 .. 1.0
  page_count    integer,
  chunk_count   integer,
  error_message text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  indexed_at    timestamptz
);
CREATE INDEX documents_user_created_idx ON documents (user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- chunks
--   Written exclusively by Instance 1. Read by Instance 2 for retrieval.
--   Retrieval MUST order by `embedding <=> :qvec` (cosine). Using <-> or <#>
--   silently abandons the HNSW index below for a sequential scan.
-- ---------------------------------------------------------------------------

CREATE TABLE chunks (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id  uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal      integer NOT NULL,             -- 0-based position within the document
  text         text NOT NULL,
  token_count  integer NOT NULL,
  page_start   integer,                      -- NULL for formats without pages (.txt, .md)
  page_end     integer,
  char_start   integer NOT NULL,             -- offset into the extracted plain text
  char_end     integer NOT NULL,
  heading_path text,                         -- e.g. "3. Methods > 3.2 Sampling"; NULL if none detected
  embedding    vector(1024) NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, ordinal)
);
CREATE INDEX chunks_document_idx ON chunks (document_id);
CREATE INDEX chunks_embedding_idx ON chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- ingestion_jobs
--   Row created by Instance 2, in the SAME transaction as its documents row.
--   All state columns written exclusively by Instance 1.
--   Claimed with SELECT ... FOR UPDATE SKIP LOCKED — see Contract 3 §3 for the
--   exact statement, including the 15-minute crash-recovery reclaim window.
-- ---------------------------------------------------------------------------

CREATE TYPE job_state AS ENUM ('queued', 'running', 'done', 'failed');

CREATE TABLE ingestion_jobs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id  uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  state        job_state NOT NULL DEFAULT 'queued',
  attempts     integer NOT NULL DEFAULT 0,
  locked_at    timestamptz,
  locked_by    text,                         -- worker instance id, for observability only
  last_error   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ingestion_jobs_claim_idx ON ingestion_jobs (state, created_at)
  WHERE state IN ('queued', 'running');

-- ---------------------------------------------------------------------------
-- conversations & messages
--   Written exclusively by Instance 2.
--   messages.citations holds the full Citation array (Contract 8) as sent to the
--   client, so a historical answer keeps showing what it was based on even after
--   its source document is deleted or re-indexed.
-- ---------------------------------------------------------------------------

CREATE TABLE conversations (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title      text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_user_created_idx ON conversations (user_id, created_at DESC);

CREATE TABLE messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            text NOT NULL CHECK (role IN ('user', 'assistant')),
  content         text NOT NULL,
  citations       jsonb NOT NULL DEFAULT '[]'::jsonb,   -- array of Citation (Contract 8)
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_conversation_idx ON messages (conversation_id, created_at);
