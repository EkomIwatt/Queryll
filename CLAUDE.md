# Queryll — Multi-Agent Coordination File

> Project 7 of 10 in the AI-accelerated full-stack ladder. Fourth Swarm run.
> Snipp proved the machinery. LedgerLite added auth and user-scoped data. TaskFlow added a
> live two-process protocol. This run adds the first **three-way split**, and with it the
> first project where **a second backend process exists** — and where correctness depends on
> two independently-written programs agreeing about the meaning of a 1024-dimensional number.

## Project goal

Queryll is a **RAG document Q&A app**. A signed-in user uploads PDFs and text documents, waits
a few seconds while they are ingested in the background, then asks questions in natural language
and gets an answer from Claude that is **grounded in their own documents and cites the exact
passages it came from**. Every claim in an answer carries a numbered citation; clicking one opens
the source passage, in context, with its page number.

Behind that sentence: parse the document → split it into overlapping, structure-aware chunks →
embed each chunk with Voyage → store the vectors in Postgres via pgvector → at question time embed
the *query*, retrieve the nearest chunks by cosine distance, assemble a grounded prompt, and stream
Claude's answer back token-by-token with the retrieved passages attached.

The engineering targets are (a) **chunking strategy** — chunks big enough to carry meaning and small
enough to be precise, split on real document structure rather than every 1000 characters;
(b) **retrieval quality** — the right passages actually surfacing for a question, with an honest
"I could not find that in your documents" when they do not; (c) **citation accuracy** — the passage
the UI highlights must genuinely be the text the answer was drawn from, not a plausible-looking
neighbour; and (d) **async ingestion** — a 40-second embedding job must not block an HTTP request,
must survive a process restart, and must be visibly in-progress to the user the whole time.

**Deliverables:** a standalone Python ingestion worker, a FastAPI + Postgres/pgvector API that
retrieves and streams grounded answers from Claude, a React/Vite/TypeScript library-and-chat UI with
streaming answers and clickable citations, three green test suites, and a clean Reconciler merge with
every frozen contract holding on all real sides.

## Stack (settled with the human — do not change without them)

| Layer | Choice |
|-------|--------|
| API | **FastAPI** + SQLAlchemy 2 (async) + asyncpg + Pydantic v2 |
| Worker | **Standalone Python process** — no web framework, no broker. Polls Postgres. |
| Queue | **Postgres job table** claimed with `SELECT ... FOR UPDATE SKIP LOCKED`. No Redis, no Celery. |
| Database | **Postgres 16 + pgvector** (Neon in prod, `pgvector/pgvector:pg16` in dev/test) |
| Vector index | **HNSW** on `vector_cosine_ops`, queried with `<=>` |
| Embeddings | **Voyage AI `voyage-4`**, 1024 dimensions, L2-normalized (200M free tokens per account) |
| Answering | **Claude `claude-sonnet-5`** via the Anthropic SDK, streamed |
| Answer transport | **SSE frames over `fetch` + `ReadableStream`** — POST, not `EventSource` |
| Auth | **argon2** (passlib) · **HS256 JWT** access token · httpOnly refresh cookie — carried over from LedgerLite/TaskFlow unchanged |
| File storage | **Postgres `bytea`** — no shared disk, no S3 |
| Frontend | **React 19** + Vite + TypeScript + react-router |
| Tests | pytest + real Postgres/pgvector (worker, API) · Vitest + Testing Library + a fake stream reader (frontend) |
| Deploy | Vercel (UI) → Render Web Service (API) + Render Background Worker (ingestion) → Neon (Postgres) |

**Six foundational decisions the human ratified before any contract was drafted:**

1. **A separate worker process, with Postgres as the queue.** Ingestion is claimed atomically with
   `FOR UPDATE SKIP LOCKED`, enqueued in the *same transaction* as the document row, and survives a
   process restart because the job is a durable row rather than an in-memory task. `BackgroundTasks`
   was rejected outright: on Render's free tier the API process restarts routinely, and it would ship
   documents that silently never finish embedding. Redis + Celery was rejected as the right tool for a
   workload Queryll does not have — one job type, no scheduling, no routing.
2. **pgvector on the existing Postgres, not Pinecone.** One datastore means a chunk and its vector are
   written in a single transaction and can never drift apart, and retrieval is ordinary SQL — which
   makes the schema a genuine frozen text contract between two instances instead of a sync problem.
3. **Voyage `voyage-4`, 1024 dimensions.** Anthropic's recommended embeddings partner, so it pairs with
   Claude for the answer step, and it distinguishes `document` from `query` input types — the asymmetry
   a real RAG pipeline should teach. Every Voyage model in both the 3.5 and 4 families defaults to 1024
   dims, so the frozen schema survives a model change (re-index the vectors; do not migrate the table).
4. **The original file lives in Postgres as `bytea`, not on disk.** This is load-bearing for the split:
   it means the API process and the worker process **share nothing but the database**. No volume, no
   bucket, no assumption that two Render services see the same filesystem. Capped at 20 MB per file.
5. **SSE over `fetch` + `ReadableStream`, not `EventSource`.** `EventSource` cannot POST and cannot set
   an `Authorization` header — the exact problem TaskFlow solved with a WebSocket ticket. `fetch` with a
   streamed response body solves both for free, so this project needs **no ticket mechanism at all**.
6. **Real auth, carried over verbatim.** LedgerLite's argon2 + HS256 JWT layer is copied unchanged so the
   new difficulty stays concentrated on RAG. Documents are strictly user-scoped: a user can only retrieve
   over, cite from, or read chunks of documents they own.

## Decomposition rationale

**Three instances. The seams are two process boundaries: worker vs. API vs. browser.**

*Could fewer instances do this?* The frontend split is not in question — different language, disjoint
directory, one HTTP surface between them, exactly as the last three runs. The real question is whether
**ingestion and retrieval are one backend or two**, and it deserved an argument, because they are both
Python, both talk to the same database, and both call the same embeddings API.

*Why the backend split holds.* Instance 1 and Instance 2 are **two separately deployed processes** with
disjoint directories (`worker/` vs `backend/`), separate `requirements.txt`, separate Dockerfiles, and
**no importable code crossing between them**. They communicate through exactly one medium — rows in
Postgres — and that medium is fully specifiable in text: the DDL (Contract 2) and the document lifecycle
state machine (Contract 3). Both directions stub cleanly. Instance 2 tests retrieval by seeding chunk rows
with fixture vectors; the worker never has to run. Instance 1 tests ingestion by inserting a document row
and a job row itself; the API never has to exist. And each side is substantial independent work: Instance 1
is PDF parsing, structure-aware chunking, embedding batching, rate-limit backoff, idempotent re-runs and
crash recovery; Instance 2 is auth, upload, retrieval SQL and index tuning, prompt assembly, citation
projection, SSE streaming and the grounding-refusal path. Rule (c): each earns its own instance.

*The duplication trap, and how it is defused.* The dangerous version of this project is the one where
**the embedding call lives on both sides with no agreement between them**. It genuinely does live on both
sides — the worker embeds chunks, the API embeds questions — and that is unavoidable, because moving
query-embedding into the worker would put a queue round-trip in the middle of a user's question. Vectors
are only comparable if both sides use the identical provider, model id, dimension and normalization. Get
that wrong on one side and nothing crashes: retrieval quietly returns near-random passages, Claude
faithfully answers from them, and **both test suites stay green**, because each side is internally
consistent. That is the worst failure mode in this project and it is invisible to unit tests by
construction.

It is defused in three layers. First, Contract 4 pins the provider, model id, dimension, input types and
normalization as literal frozen values, read from the same env var names with the same defaults on both
sides. Second, both sides must assert at runtime — not in a comment — that every vector they produce has
`len(vec) == 1024` and `norm(vec) ~= 1.0`, and must fail loudly rather than store or query with a
wrong-shaped vector. Third, because no pre-merge test on either side can prove the two agree, a
**cross-process cosine probe is a mandatory ★ merge-time check**: embed the same probe string through the
worker's client and through the API's client and assert cosine ≥ 0.99. The trap is not eliminated — it is
converted from a silent quality problem into a single named check that fails loudly at a known moment.

*The second duplication — the SQLAlchemy models — is deliberate and bounded.* Both Python instances map the
same tables. This is ordinary practice for two services over one database, and it is safe here for one
reason: **neither instance owns the schema.** `db/init.sql` is Contract 2, and it is ratified and committed
to `main` by the human *before the worktrees are created*, exactly as the contract text is. Both instances
therefore branch from byte-identical DDL, both declare only the columns they touch, and neither runs a
migration the other has not seen. Alembic is explicitly out of scope for this run — `init.sql` is the single
source of truth. A schema change is an ESCALATION, not an edit.

*Why the frontend is one instance and not two.* An obvious-looking cut is "library UI" vs "chat UI", and it
is wrong: they share the auth shell, the API client, the router, the design tokens, and the citation
component that the chat renders and the library viewer opens. Splitting them would put two agents in `src/`
writing the same files. Rule (a): one owner.

*Why no fourth instance for glue.* `docker-compose.yml` (the `db` service only) ships with the DDL on `main`
as part of Contract 2, since all three instances need a database to develop against and none of them may own
it. Each instance writes its own Dockerfile and its own `.env.example` inside its own directory. The assembled
production compose file, `DEPLOY.md`, `BUILT-WITH-SWARM.md` and the root `README.md` are **merge-time artifacts
for the human/Reconciler** (rule b): small, spanning all three, and best written once all three sides are real.
Nothing cross-cutting here is substantial enough to earn an instance of its own.

**Nothing is deliberately sequenced.** All three instances build fully in parallel behind the frozen contracts.

**Five boundaries that cannot be proven until merge** — known and scoped in advance, written up as ★ checks in
MERGE-TIME ARTIFACTS & CHECKS at the bottom of this file:

1. **Vector comparability** across the two independently-written embedding clients (above).
2. **Real ingestion of a real PDF** — page-number accuracy, chunk boundaries and heading detection against an
   actual multi-column, header-and-footer-bearing PDF, not a synthetic fixture.
3. **Citation truthfulness** — that the passage the UI opens is genuinely the text the answer came from.
   Instance 2 can prove the *mapping* is consistent; only a human reading a real answer can prove it is *true*.
4. **SSE survives the real deploy path.** Streaming is routinely broken by proxy buffering and by compression
   middleware that coalesces frames. Only a real origin proves it.
5. **The HNSW index is actually used.** pgvector silently falls back to a sequential scan when the query's
   distance operator does not match the index's operator class. Green tests on 50 fixture rows prove nothing.

## Status legend
IN PROGRESS · PENDING · BLOCKED · DONE · ASSUMED · WAITING ON <who/what>

## Project-wide conventions (binding on all three instances)

- **The database is the only medium between Instance 1 and Instance 2.** No HTTP between them, no shared
  filesystem, no shared Python package, no importing across `worker/` and `backend/`. If you find yourself
  wanting a function the other instance has, you have found either a contract gap (escalate) or a thing you
  should write yourself.
- **Column ownership is absolute** (Contract 3). Every mutable column has exactly one writer. Reading another
  instance's column is fine; writing it is a contract violation even when it would "obviously work".
- **A vector is never trusted on shape alone.** Both Python instances assert `len(vec) == EMBEDDING_DIM` and
  `abs(norm(vec) - 1.0) < 1e-3` at the boundary where a vector enters the program, and raise on failure.
- **Cosine distance, always: `<=>`.** Never `<->` (L2) and never `<#>` (inner product). The HNSW index is built
  `vector_cosine_ops`; a mismatched operator silently degrades to a sequential scan rather than erroring.
- **`chunk.id` is the citation primary key.** Citations reference chunks by id — never by page number, ordinal,
  text prefix, or similarity rank. Ordinals and pages are display metadata.
- **Answers cite with `[n]` markers, 1-indexed into the retrieval list sent first.** The model is instructed to
  emit them and the API validates them; a marker with no corresponding source is stripped before it reaches the
  client, never passed through.
- **Grounding is mandatory.** If no retrieved chunk clears the similarity floor, the API does not ask Claude to
  answer from general knowledge — it takes the `insufficient_context` path (Contract 7 §5). An answer with zero
  citations is a bug, not a style choice.
- **Error envelope is `{ "error": "<sentence>" }`** for every non-2xx HTTP response, from every endpoint. Errors
  inside an already-open SSE stream use the `error` event instead (Contract 9).
- **A document belonging to someone else returns 404, never 403** — carried from LedgerLite. Ownership must not
  be discoverable by probing ids. This applies to chunks and conversations too.
- **The client never sends a user id.** Ownership is derived from the access token, always.
- **Timestamps** are ISO-8601 UTC with a trailing `Z`.
- **The dev database is on host port 5433, not 5432.** Snipp (project 1) already holds 5432 on this machine, so
  Queryll is mapped one port up and both run side by side. `docker compose up -d db`, then
  `DATABASE_URL=postgresql+asyncpg://queryll:queryll@localhost:5433/queryll` in your `.env.example`.
- **No WebSockets in this project.** Ingestion progress is polled (Contract 6 §4). This is deliberate: TaskFlow
  taught the live-protocol lesson, and a socket here would buy a smoother progress bar at the cost of a second
  transport to specify, test and deploy.

---

## INTERFACE CONTRACTS  —  STATUS: FROZEN

**Ratified by the human on 2026-09-07 and committed to `main` before any worktree was created**, so all
three instances branch from byte-identical contract text and byte-identical DDL.

<!-- FROZEN. No instance may edit anything in this block, for any reason — including to fix
     something that is obviously wrong. Write a proposed amendment in ESCALATIONS & PROPOSED
     AMENDMENTS below and wait for the human, who is the only one who edits this block. -->

### Contract 1: Authentication  (carried over from LedgerLite / TaskFlow, unchanged)

- **Producer:** Instance 2 · **Consumer(s):** Instance 3

```
POST /api/auth/signup   { "email": str, "password": str }        -> 201 { "access_token": str, "user": User }
POST /api/auth/login    { "email": str, "password": str }        -> 200 { "access_token": str, "user": User }
POST /api/auth/refresh  (httpOnly cookie, no body)               -> 200 { "access_token": str, "user": User }
POST /api/auth/logout   (httpOnly cookie)                        -> 204
GET  /api/auth/me       (Bearer)                                 -> 200 User

User = { "id": str, "email": str, "display_name": str, "created_at": iso8601 }
```

- Access token: HS256 JWT, 15-minute expiry, sent as `Authorization: Bearer <token>`.
- Refresh token: httpOnly, Secure, SameSite=None cookie, 30-day expiry, rotated on every refresh.
- Passwords: argon2 via passlib. Minimum 8 characters. Never logged, never returned.
- `display_name` falls back to the email local-part. **ASSUMED** — display-only, low stakes.

### Contract 2: Database schema  (`db/init.sql`)

- **Producer:** ratified artifact committed on `main` — owned by **no instance** after ratification.
- **Consumer(s):** Instance 1 (writes chunks, updates document state), Instance 2 (writes documents, reads chunks)
- Both Python instances declare SQLAlchemy models for **only the tables they touch**, mapping this DDL exactly.
  No Alembic. No `create_all` against a real database. A schema change is an ESCALATION.

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  display_name  text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

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
```

**Deliberate omissions, for the record:** no `updated_at` on `documents` (status transitions are the
audit trail); no soft deletes (a deleted document is gone, and `ON DELETE CASCADE` takes its chunks with
it); no per-chunk provider/model column (the model is global config — a model change is a full re-index,
tracked in `DEPLOY.md`, not per row).

### Contract 3: Document lifecycle & the ingestion job protocol  ★ the load-bearing contract

- **Producer/Consumer:** Instance 1 and Instance 2 are **peers** across this contract. It is the entire
  interface between two processes that never call each other.

**§1 — Column ownership. Every mutable column has exactly one writer.**

| Column | Written by | Read by |
|---|---|---|
| `documents.id / user_id / filename / mime_type / size_bytes / content / created_at` | **Instance 2** (insert only) | Instance 1 |
| `documents.status / progress / page_count / chunk_count / error_message / indexed_at` | **Instance 1** | Instance 2 |
| `chunks.*` (all rows) | **Instance 1** | Instance 2 |
| `ingestion_jobs` row creation | **Instance 2** | Instance 1 |
| `ingestion_jobs.state / attempts / locked_at / locked_by / last_error / updated_at` | **Instance 1** | Instance 2 (read-only, for a stuck-job admin view) |
| `conversations.* / messages.*` | **Instance 2** | — |

**The one ratified exception:** on re-index (Contract 6 §5) Instance 2 sets `documents.status = 'pending'`,
`progress = 0.0`, `error_message = NULL`, deletes that document's chunks, and inserts a fresh job row — all in
one transaction. This is the only circumstance in which Instance 2 writes a worker-owned column, and it is
only legal when the document's current status is `ready` or `failed` (never `processing`).

**§2 — Enqueue is transactional.** Instance 2 inserts the `documents` row and its `ingestion_jobs` row in the
**same transaction**. A committed document without a job is an unreachable state, not a race to paper over.

**§3 — Claiming. Exactly this shape:**

```sql
UPDATE ingestion_jobs SET state = 'running', attempts = attempts + 1,
       locked_at = now(), locked_by = :worker_id, updated_at = now()
WHERE id = (
  SELECT id FROM ingestion_jobs
  WHERE state = 'queued'
     OR (state = 'running' AND locked_at < now() - interval '15 minutes')  -- crash recovery
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

Two workers running concurrently must never claim the same job. The 15-minute reclaim window is how a job
orphaned by a killed process comes back; it is why ingestion must be **idempotent** (§5).

**§4 — Legal state transitions.** `documents.status`:

```
pending ──(worker claims)──> processing ──(all chunks written)──> ready
                                  │
                                  ├──(unrecoverable, or attempts >= 3)──> failed
                                  └──(transient failure, attempts < 3)──> pending
ready ─────(re-index, Instance 2)──> pending
failed ────(re-index, Instance 2)──> pending
```

**AMENDED 2026-09-08** (ratified by the human at merge; raised by Instance 1 before it). The retry edge
`processing -> pending` is explicit above. When a job fails *transiently* and is requeued with
`attempts < 3`, the document returns to `pending` — "waiting for a worker", which is exactly true once
no worker holds it — and `progress` resets to 0.0 with it, which is the transition §7 already
anticipated. This is not user-visible: Contract 6 §4 has Instance 3 polling on both `pending` and
`processing`, and both render as in-progress. It is visible to a stuck-job admin view, which is why it
is pinned rather than left to two independent guesses.

Nothing leaves `ready` except by re-index or deletion. `progress` moves monotonically upward within a single
`processing` run and resets to 0.0 only on a transition back to `pending`. A `failed` document **must** have a
non-null, human-readable `error_message` — that string is rendered verbatim in the UI, so it is user-facing
copy, not a stack trace.

**§5 — Ingestion is idempotent.** A job may run more than once (crash reclaim, retry). Before writing chunks,
the worker deletes all existing chunks for that document in the same transaction as the new insert. Re-running
a job on an already-`ready` document must produce the same end state, never duplicate chunks. Because chunking
is deterministic (Contract 5 §4), a re-run also produces the *same chunk ordinals* — though chunk **ids change**,
which is why citations are resolved at answer time and never cached across a re-index.

**§6 — Retries.** `attempts >= 3` on the same job means `state = 'failed'` and `documents.status = 'failed'`.
Transient failures (Voyage 429/5xx, connection resets) are retried inside a single run with exponential backoff
before the attempt is counted as spent. Permanent failures (encrypted PDF, unparseable file, zero extracted text)
go straight to `failed` without burning three attempts.

**§7 — Progress semantics.** `progress` is the fraction of chunks embedded, written after each embedding batch:
`embedded_chunks / total_chunks`, clamped to `[0.0, 0.99]` while `processing`. It reaches `1.0` only together
with `status = 'ready'`. Parsing and chunking happen before any chunk count is known, so they report `0.0` —
Instance 3 renders an indeterminate state for `processing` at `progress == 0.0`.

### Contract 4: Embedding specification  ★ the silent-divergence contract

- **Consumers:** Instance 1 (embeds chunks) **and** Instance 2 (embeds questions). Both implement this
  independently; neither may import the other's client.

```
provider          Voyage AI  (https://api.voyageai.com/v1/embeddings)
model             voyage-4                    env EMBEDDING_MODEL,  default "voyage-4"
dimension         1024                        env EMBEDDING_DIM,    default 1024
input_type        "document" for chunks  |  "query" for questions   ← the asymmetry; do not skip it
output_dtype      float (default)
normalization     Voyage returns L2-normalized vectors. Store as returned. Do not re-normalize.
distance          cosine  ->  pgvector `<=>`
similarity        similarity = 1.0 - (embedding <=> query_vector)     ∈ [0, 2] in theory, [0, 1] in practice
```

**Mandatory runtime assertions on both sides**, at the boundary where a vector enters the program:

```python
assert len(vec) == EMBEDDING_DIM, f"expected {EMBEDDING_DIM} dims, got {len(vec)}"
assert abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < 1e-3, "vector is not L2-normalized"
```

Raise, do not warn, and never store or query with a vector that fails either check.

- **Batching:** at most 128 inputs per request and at most the model's 32,000-token context per input.
  Truncate a single over-long input rather than dropping it, and log when you do.
- **Rate limits:** exponential backoff with jitter on 429 and 5xx — 1s, 2s, 4s, 8s, then surface the failure.
- **`input_type` support must be verified against the live API on first implementation.** If the parameter is
  rejected, **escalate** — do not silently drop it. Dropping it on one side only is precisely the asymmetry
  this contract exists to prevent.
- **Test-suite rule:** no test may call the live Voyage API. Both instances use a deterministic fake embedder
  (e.g. seeded hash → 1024 floats → L2-normalize) that satisfies both assertions. The real client is exercised
  only by the ★ merge-time probe.

### Contract 5: Chunking specification

- **Producer:** Instance 1 · **Consumer(s):** Instance 2 (projects chunks into citations), Instance 3 (renders them)

**§1 — Supported inputs.** `application/pdf`, `text/plain`, `text/markdown`. Anything else is rejected at upload
by Instance 2 with 415 (Contract 6 §1). Max 20 MB, max 500 pages. **ASSUMED** — limits are tunable config, and
they are chosen so a worst-case document embeds inside the 15-minute reclaim window.

**§2 — Target shape.** ~512 tokens per chunk, ~64 tokens of overlap (12.5%) with the previous chunk. Chunks
below 32 tokens are merged forward rather than stored alone.

**§3 — Split on structure first, size second.** Split at headings, then paragraph breaks, then sentence
boundaries, and only pack to the token target within those units. Never split mid-sentence unless a single
sentence exceeds the target. Markdown headings come from `#` levels; PDF headings are detected by font-size
and weight relative to body text, falling back to `heading_path = NULL` when detection is unreliable —
**a wrong heading is worse than no heading**, because it ends up printed under a citation as if it were fact.

**§4 — Determinism.** The same bytes must always produce the same chunk boundaries, ordinals, offsets and
token counts. No randomness, no wall-clock, no dict-ordering dependence. This is what makes re-index safe (§5
of Contract 3) and what lets a chunking bug be reproduced from the stored `content` alone.

**§5 — Metadata is a promise to the citation UI.** `char_start`/`char_end` index into the *extracted plain
text* of the document, and `text` must equal that slice exactly. `page_start`/`page_end` are 1-based and
inclusive; a chunk spanning a page break carries both. For `.txt`/`.md` both are NULL and the UI shows no page
chip. **PDF header/footer lines (running titles, page numbers) are stripped before chunking** — they otherwise
appear in every chunk, poison the embeddings with repeated boilerplate, and read as nonsense inside a citation.

### Contract 6: Documents HTTP API

- **Producer:** Instance 2 · **Consumer(s):** Instance 3 · All routes require `Authorization: Bearer <token>`.

```
Document = {
  "id": str, "filename": str, "mime_type": str, "size_bytes": int,
  "status": "pending" | "processing" | "ready" | "failed",
  "progress": float,            // 0.0 .. 1.0
  "page_count": int | null, "chunk_count": int | null,
  "error_message": str | null,  // non-null iff status == "failed"; render verbatim
  "created_at": iso8601, "indexed_at": iso8601 | null
}
```

**§1 — Upload.** `POST /api/documents`, `multipart/form-data`, field name `file`.
→ `202 Accepted` with a `Document` at `status: "pending"`. The response is returned **before** any parsing,
embedding or page counting happens; `page_count` and `chunk_count` are therefore `null` here, always.
→ `413` if over 20 MB · `415` if the MIME type is unsupported · `422` if the field is missing.

**§2 — List.** `GET /api/documents?limit=50&cursor=<opaque>` → `200 { "documents": [Document], "next_cursor": str|null }`
Newest first. Only the caller's own documents, ever.

**§3 — Detail & passages.**
```
GET /api/documents/{id}                        -> 200 Document          | 404
GET /api/documents/{id}/chunks?limit=&cursor=  -> 200 { "chunks": [ChunkSummary], "next_cursor": str|null }
GET /api/documents/{id}/chunks/{chunk_id}      -> 200 { "chunk": Chunk, "prev": Chunk|null, "next": Chunk|null }

ChunkSummary = { "id": str, "ordinal": int, "page_start": int|null, "page_end": int|null,
                 "heading_path": str|null, "preview": str }   // preview: first 200 chars
Chunk        = ChunkSummary + { "text": str, "token_count": int }
```
The single-chunk route returns its immediate neighbours so the citation viewer can show a passage **in context**
without fetching the whole document. Embeddings are never serialized to the client — not in any route, ever.

**§4 — Polling contract.** Instance 3 polls `GET /api/documents` every **2 seconds** while any document is
`pending` or `processing`, and stops entirely when none is. After 5 minutes of continuous polling the interval
backs off to 10 seconds. No polling on a fully-`ready` library — an idle tab must issue zero requests.

**§5 — Re-index.** `POST /api/documents/{id}/reindex` → `202` with the `Document` reset to `pending`.
`409` if the document is currently `processing`. Performs the Contract 3 §1 exception transaction.

**§6 — Delete.** `DELETE /api/documents/{id}` → `204`. Cascades to chunks. `409` if `processing` — a delete
racing a running worker is refused rather than resolved. Deleting a document does **not** delete past
conversation messages that cite it; those citations become dangling and Contract 8 §3 governs how they render.

### Contract 7: Ask — retrieval and the streamed answer  ★ the centrepiece

- **Producer:** Instance 2 · **Consumer(s):** Instance 3

**§1 — Transport.** `POST /api/conversations/{id}/ask`, `Authorization: Bearer <token>`,
`Accept: text/event-stream`. The response is `text/event-stream` read via `fetch` + `ReadableStream`.
**Not `EventSource`** — it cannot POST and cannot carry the auth header. Server sends
`Cache-Control: no-cache`, `X-Accel-Buffering: no`, and **no compression middleware on this route**.

```
Request: { "question": str, "document_ids": [str] | null }   // null = search all of the user's documents
```

**§2 — Frame format.** Standard SSE: `event: <name>\ndata: <json>\n\n`. Every `data` is a single-line JSON
object. A comment heartbeat `: ping\n\n` is sent every 15 seconds while the model is thinking, so an idle proxy
does not close the connection.

**§3 — Event sequence. Strictly ordered:**

```
event: retrieval      { "sources": [Citation], "insufficient_context": bool }   // ALWAYS FIRST, exactly once
event: token          { "text": "..." }                                        // zero or more, in order
event: done           { "message_id": str, "citations_used": [int] }            // exactly once, terminal
event: error          { "error": "<sentence>", "code": str }                    // terminal, replaces `done`
```

`retrieval` is sent **before the model is called**, so the UI paints the sources it is about to answer from
while the answer is still being generated. `citations_used` lists the 1-based indices actually referenced in
the final text, so the UI can dim sources the answer did not use.

**§4 — Retrieval parameters.**

```
top_k              8 chunks                        env RETRIEVAL_TOP_K
similarity_floor   0.35                            env RETRIEVAL_MIN_SIMILARITY
per_document_cap   4 chunks                        // no single document may fill the whole context
ordering           similarity DESC, then (document_id, ordinal) ASC as a deterministic tie-break
scope              WHERE documents.user_id = :caller  AND (document_ids IS NULL OR document_id = ANY(:ids))
```

The floor and `top_k` are **ASSUMED** starting values — tuning them against real documents is expected work,
and the values are config, not constants in the code.

**§5 — The grounding rule.** If no chunk clears `similarity_floor`, the API sends
`retrieval { "sources": [], "insufficient_context": true }`, then a single `token` event with a fixed sentence —
`"I could not find anything about that in your documents."` — then `done`. **Claude is not called at all.**
Answering from general knowledge when retrieval fails is the single worst thing this app can do, and the cheapest
way to guarantee it never happens is to not make the call.

**§6 — Prompt assembly.** Retrieved chunks go in the system prompt wrapped as numbered sources, each carrying its
filename, page range and heading path. The instruction set is: answer only from the sources; cite every claim with
`[n]`; if the sources do not contain the answer, say so plainly; never invent a source number. Prior turns in the
conversation are included as message history, but **prior turns' retrieved chunks are not re-sent** — each question
retrieves fresh. Conversation history is capped at the last 6 messages. **ASSUMED** — tunable.

**§7 — Marker validation.** Before a `token` event leaves the server, `[n]` markers are validated against the
sources actually sent. A marker outside `1..len(sources)` is **stripped from the text**, not passed through and not
rendered as a broken link. Because markers can straddle a stream boundary, the API buffers a small tail (any
trailing partial `[`…) rather than emitting a half-marker.

**§8 — Persistence.** The user message is written before the stream opens. The assistant message is written on
`done`, with its `citations` column holding the full `Citation` array. A client that disconnects mid-stream still
gets a persisted message — generation is not cancelled by a dropped reader, so a refresh shows the completed answer.

**§9 — Conversations.**
```
POST   /api/conversations              { "title": str|null }  -> 201 Conversation
GET    /api/conversations              -> 200 { "conversations": [Conversation] }
GET    /api/conversations/{id}         -> 200 { "conversation": Conversation, "messages": [Message] }
DELETE /api/conversations/{id}         -> 204

Conversation = { "id": str, "title": str|null, "created_at": iso8601 }
Message      = { "id": str, "role": "user"|"assistant", "content": str,
                 "citations": [Citation], "created_at": iso8601 }
```
`title` is auto-set from the first question (first 60 chars) when created as `null`. **ASSUMED** — display-only.

### Contract 8: The Citation object

- **Producer:** Instance 2 (projected from Instance 1's chunk metadata) · **Consumer(s):** Instance 3

```
Citation = {
  "index": int,             // 1-based; the [n] in the answer text
  "chunk_id": str,          // the stable handle — resolve passages by this, never by index or page
  "document_id": str,
  "filename": str,
  "page_start": int | null,
  "page_end": int | null,
  "heading_path": str | null,
  "similarity": float,      // 0..1, rounded to 3dp — display and debugging
  "snippet": str            // <= 300 chars of the chunk text, for the inline hover card
}
```

**§1 — `index` is per-answer, not global.** It is meaningful only within the message it was sent with.
**§2 — Clicking a citation opens `GET /api/documents/{document_id}/chunks/{chunk_id}`** (Contract 6 §3) for the
full passage with neighbours. `snippet` is for the hover card only and is never the source of truth for the passage.
**§3 — Dangling citations.** A citation whose document has since been deleted or re-indexed returns 404 from the
chunk route. Instance 3 renders the stored `filename` and `snippet` with a "source no longer available" note. It
does not error, and it does not hide the citation — a historical answer keeps showing what it was based on.

### Contract 9: Errors, status codes, streaming failures and CORS

- **Producer:** Instance 2 · **Consumer(s):** Instance 3

| Situation | Response |
|---|---|
| Validation failure | `422 { "error": "<sentence>" }` |
| Missing/expired access token | `401 { "error": "..." }` — Instance 3 refreshes once, then redirects to login |
| Another user's document / conversation / chunk | `404`, never `403` |
| Upload over 20 MB | `413` |
| Unsupported MIME type | `415` |
| Delete or re-index while `processing` | `409` |
| Voyage unavailable at query time | `503 { "error": "Search is temporarily unavailable. Try again in a moment." }` |
| Anthropic error *before* the stream opens | `502` + JSON envelope |
| Anthropic error *after* the stream opens | `event: error` inside the stream — the HTTP status is already 200 |

- **`error` is always one human-readable sentence**, safe to render directly. No stack traces, no SQL, no provider
  payloads, no key fragments — ever, including in `documents.error_message`.
- **CORS:** credentials allowed; origin allow-list from `CORS_ORIGINS`; must include the Vercel production **and**
  preview domains. `Authorization` and `Content-Type` on the allowed-header list.
- **Never log:** file bytes, extracted document text, embeddings, API keys, or JWTs. Document *ids* are fine.

---

## ESCALATIONS & PROPOSED AMENDMENTS

<!-- Instances write structured requests + *proposed* amendments here. Never edit the frozen
     contract block or another instance's section directly. The human resolves. -->

### [AMENDMENT] 2026-09-07 — Instance 2
**Type:** gap
**Re:** Contract 7 §3 — the `error` SSE event
**Issue:** The frame is specified as `event: error { "error": "<sentence>", "code": str }`, but the
legal values of `code` are never enumerated anywhere in the contracts. `error` is already defined as a
human-readable sentence that is safe to render, so `code` can only exist for the client to branch on —
and Instance 3 is building its stream reader against this shape right now without being told what it
may branch on. Left unpinned, Instance 3 invents a set, I invent a different set, and the mismatch
stays invisible until a real mid-stream failure in production.
**Proposed resolution:** Add to Contract 7 §3 the closed set this instance currently emits —
`"retrieval_unavailable"` (Voyage failed after the stream opened), `"answer_unavailable"` (Anthropic
errored, was unreachable, or declined), `"internal_error"` (anything else) — plus the standing rule
that Instance 3 renders `error` verbatim and treats an unrecognised `code` as `internal_error`, so the
set can be extended later without breaking a deployed client.
**Blocked work:** none — continuing other work. The API emits those three codes today and the `error`
sentence is renderable without `code` at all.
**Status:** OPEN

### [ESCALATION] 2026-09-07 — Instance 2
**Type:** question
**Re:** Contract 4 — "`input_type` support must be verified against the live API on first implementation"
**Issue:** I cannot perform this verification. It needs a live `VOYAGE_API_KEY`, which is not present
in this worktree, and spending the human's Voyage quota on a third-party API call is not a decision I
should take unprompted. The contract assigns the check to first implementation, so I am flagging that
it has NOT happened rather than leaving it looking done.
**Proposed resolution:** human to decide — either make one live call to
`https://api.voyageai.com/v1/embeddings` with `{"model": "voyage-4", "input_type": "query",
"output_dtype": "float", "input": ["probe"]}` and confirm a 1024-dimension unit vector comes back, or
accept that ★ merge-time check 5 already covers it, since the cross-process cosine probe exercises
both real clients and would fail if either side's `input_type` were being rejected.
**Blocked work:** none — continuing other work. What exists instead: `input_type: "query"` is always
sent, and a 400 whose body mentions `input_type` raises `VoyageInputTypeRejected` rather than retrying
without the parameter — Contract 4 requires an escalation at that point, not a silent fallback, and
dropping it on the query side alone would decorrelate every query vector from every stored chunk vector.
**Status:** RESOLVED 2026-09-08 (human, at merge) — **deferred to the ★ cross-process cosine probe.** No
separate live call is made now. The probe exercises both real clients end to end and would fail if either
side's `input_type` were rejected, so it subsumes this check. Both clients already fail loudly rather than
dropping the parameter, which is what makes deferring it safe. Remains a pre-launch gate, not a closed item.

### AMENDMENT 2026-09-07 — Instance 1
**Type:** proposed-amendment
**Re:** Contract 3 §4 (legal `documents.status` transitions)

**Issue:** The state diagram gives `processing` exactly two exits — `ready` and `failed` — and
shows `pending` being re-entered only by Instance 2's re-index. It does not say what
`documents.status` should be when a job fails *transiently* and is put back on the queue with
`attempts < 3` (Contract 3 §6). That path is real and routine: a Voyage 503 that survives the
backoff schedule costs the job one attempt and the job returns to `queued`, but no worker is
holding the document any more.

Two readings, and the contract supports each in a different place:

* **`processing` stays.** §4's diagram looks exhaustive, and the document is conceptually still
  mid-ingestion.
* **Back to `pending`.** §7 says progress "resets to 0.0 only on a transition back to
  `pending`", which implies transitions back to `pending` exist beyond re-index — and a retry is
  precisely where progress must reset, because the next run starts from zero.

I implemented the second reading (`processing -> pending`, progress 0.0, job `queued`), on the
grounds that `pending` means "waiting for a worker", which is exactly true after a requeue, and
that §7 anticipates it. **This is not user-visible**: Contract 6 §4 has Instance 3 polling on
both `pending` and `processing`, and both render as in-progress. It *is* visible to any
stuck-job admin view Instance 2 builds, which is why it is worth pinning down rather than
leaving to two independent guesses.

**Proposed resolution:** add the retry edge to the §4 diagram, making the implemented behaviour
explicit:

```
pending ──(worker claims)──> processing ──(all chunks written)──> ready
                                  │
                                  ├──(unrecoverable, or attempts >= 3)──> failed
                                  └──(transient failure, attempts < 3)──> pending
ready ─────(re-index, Instance 2)──> pending
failed ────(re-index, Instance 2)──> pending
```

If the human prefers the first reading instead, the change on this side is one line in
`worker/queryll_worker/queue.py::requeue_job` (`reset_document=False`), plus the two tests that
assert it.

**Blocked work:** none — implemented under the second reading, tested, and documented in the
Instance 1 work log. Raised so the merge does not discover two instances assuming different
state machines.
**Status:** RESOLVED 2026-09-08 (human, at merge) — **accepted as proposed.** Contract 3 §4 has been
amended to show the retry edge. Instance 1's implementation already matches the ratified reading, so no
code changed on either side; the Reconciler verified `requeue_job(reset_document=True)` is the default
in `worker/queryll_worker/queue.py:268`.

---

## INSTANCE 1 — Ingestion Worker & the Vector Pipeline  ·  STATUS: DONE

**Owns:** `worker/` in full — the poll loop, job claiming, document parsing (PDF/txt/md), text extraction,
header/footer stripping, chunking, the Voyage embedding client, chunk writes, progress and status updates,
retry/backoff, crash recovery; plus `worker/tests/`, `worker/requirements.txt`, `worker/Dockerfile`,
`worker/.env.example`, `worker/README.md`.

**Does NOT touch:** `backend/` (anything at all), `frontend/` (anything at all), `db/init.sql`,
`docker-compose.yml`, the root `README.md`, `DEPLOY.md`, `BUILT-WITH-SWARM.md`, the INTERFACE CONTRACTS block,
or another instance's section of this file.

**Assigned skills:** `rag-architect`, `python-pro`, `postgres-pro`, `ml-pipeline`, `test-master`
(these live under the `fullstack-dev-skills:` prefix)

**Role prompt:**

You are **Instance 1**, owner of the Queryll ingestion worker. You build a standalone Python process — no web
framework, no HTTP surface, no broker — that turns uploaded bytes into searchable, citable vectors. You own the
two things that decide whether this product is any good: **the chunking strategy** and **the embedding pipeline**.

You are the **producer of Contract 5** and a **peer party to Contracts 3 and 4**. Instance 2 is building
retrieval against chunk rows it seeds itself, with fixture vectors, and will never see your code. Instance 3 is
rendering citations built entirely from metadata *you* write. The contract text is the specification, not a sketch.
If you believe a contract is wrong, do not "improve" it silently — write a proposed amendment in ESCALATIONS and
wait for the human.

**Chunking is your core work, and it is a quality problem, not a plumbing problem.**

- Contract 5 is the spec. Split on structure first and size second; a chunk that ends mid-clause retrieves badly
  and reads as broken when a user opens it under a citation.
- **`char_start`/`char_end` must slice the extracted text back to exactly `chunk.text`.** Property-test this over
  every fixture: extract, chunk, then assert `extracted[c.char_start:c.char_end] == c.text` for every chunk. This
  invariant is what makes citations trustworthy, and it is very easy to break with an off-by-one during overlap.
- **Strip PDF running headers and footers before chunking.** Detect lines that repeat at the same vertical position
  across most pages. Left in, they appear in every single chunk, drag every embedding toward the same boilerplate,
  and surface inside citations as noise. This is the highest-leverage 30 lines in the project.
- **Page attribution must survive chunking.** A chunk that spans a page break carries `page_start` and `page_end`
  and they must be right — a citation pointing at the wrong page is worse than one with no page at all.
- **Determinism is a hard requirement** (Contract 5 §4). Same bytes → same chunks, forever. Test it by chunking the
  same fixture twice in one process and once in a fresh one, and comparing the full chunk list.
- Build a small **retrieval-quality harness** for yourself: a fixture document, a handful of questions, and the
  chunk you believe should win. It is the only way to tell whether a chunking change helped or hurt, and it is worth
  more than any unit test in this instance.

**The embedding client — Contract 4, and the assertions are not optional.**

- `input_type="document"` for chunks. The query side is Instance 2's; you never embed a query.
- Assert dimension and L2 norm on every returned vector and **raise**. A wrong-shaped vector that reaches the
  database is a silent, project-wide correctness failure that no test on either side will catch.
- Batch up to 128 inputs, back off exponentially on 429/5xx, and count an attempt as spent only after backoff is
  exhausted (Contract 3 §6).
- **No test calls the live Voyage API.** Use the deterministic fake embedder. The real client is proven once, at
  merge, by the ★ cosine probe.

**The job loop — Contract 3, and it must survive being killed.**

- Claim with the exact `FOR UPDATE SKIP LOCKED` statement in Contract 3 §3. **Test concurrency for real**: start two
  workers against one queued job and assert exactly one claims it. Do not verify this by reading the code.
- **Idempotence over cleverness.** Delete this document's chunks and insert the new ones in one transaction. A job
  that runs twice must leave the same database as a job that ran once — that is what makes the 15-minute reclaim
  window safe rather than a duplicate-chunk generator.
- **Write progress often enough to be believable.** After every embedding batch, per Contract 3 §7. A progress bar
  that sits at 0 for 40 seconds and then jumps to done is, to the user, indistinguishable from a hang.
- **`error_message` is user-facing copy.** "This PDF is password-protected and could not be read." — not a traceback,
  not a provider error string, and never anything containing a key or a file path.
- Handle the ugly real cases explicitly and permanently-fail them without burning three attempts: encrypted PDFs,
  scanned image-only PDFs with no extractable text, zero-byte files, and files whose extension lies about content.
  **OCR is out of scope** — a scanned PDF fails with a clear message saying so.
- Run one worker in production (Render Background Worker). The claim query is nonetheless correct under N workers,
  and your tests prove it, so scaling later is a config change and not a rewrite.

**Testing:** pytest against **real Postgres with pgvector** (`docker compose up db`) for anything touching SQL —
claiming, transactions, cascades. Parsing, chunking and offset invariants need no database and should be fast and
numerous. Commit at least one genuinely messy PDF fixture; a clean single-column PDF proves almost nothing.

Follow the `swarm-worker` runtime protocol for all shared-file, escalation, and git rules.

**Work log:**

**2026-09-07 — Instance 1 — STATUS: DONE.** Worker built, self-tested against the contracts,
and exercised as a real process. 153 tests pass (36 of them against real Postgres/pgvector),
`ruff` clean, `mypy --strict` clean on all 24 source files.

*What was built, and where.* Everything lives under `worker/`, deployed as a Render Background
Worker from `worker/Dockerfile` (no port, no health check). `parsing/` turns bytes into one
canonical text — PDF column detection, running header/footer stripping, font-based heading
detection, plus plain text and Markdown. `chunking/` splits that text on structure first and
size second. `embeddings/` holds the Voyage client and the shared deterministic fake.
`queue.py` carries the Contract 3 §3 claim statement verbatim and every write this process
makes. `pipeline.py` and `runner.py` are per-job orchestration and the poll loop. `quality.py`
plus `tools/quality_harness.py` are the retrieval-quality harness. `worker/README.md` explains
the reasoning; this is the summary.

*The decision worth knowing about.* **Chunks are spans, never rebuilt strings.** Every unit the
packer moves is a `(char_start, char_end)` pair into the canonical text, and a chunk is
`(first_unit.start, last_unit.end)`. Contract 5 §5's invariant —
`text[c.char_start:c.char_end] == c.text` — is therefore true *by construction* rather than by
careful arithmetic, which matters because the overlap logic is exactly where that invariant is
normally lost to an off-by-one. It is property-tested over every fixture, and again through the
database in `test_pipeline.py`.

*Contract 4 is enforced, not commented.* Vectors are checked for dimension and unit norm at the
boundary where they enter the program, written as an explicit `raise` rather than an `assert`
(which `python -O` strips). A violation is **fatal to the process**: the runner requeues the job
untouched, logs `CONTRACT 4 VIOLATION`, and exits non-zero. Failing documents one at a time
would hide the real fault. `input_type="document"` is never dropped — a rejection surfaces a
message saying to escalate. **No test calls the live Voyage API.**

*Four defects that only the real thing surfaced,* all fixed and all now covered by tests:

1. **Column detection looked for an *empty* gutter.** A full-width title puts ink straight
   through one, so every two-column page read as single-column and the columns interleaved into
   alternating half-sentences. The gutter is now the widest *low-coverage* valley, counted per
   row, and a crossing row is classified by what it is anchored to.
2. **Running headers were stripped after column splitting**, so a centred running title was cut
   in half before it could be recognised as boilerplate. That pass now runs on rows, before
   columns are worked out.
3. **Chunking was quadratic.** `_is_boundary` sliced `text[:punct_index]` at every candidate
   sentence boundary, copying the whole document once per sentence: a 2 MB text file took over
   seven minutes. Bounded to a 48-character look-behind — the same file now chunks in 1.5s.
   Contract 5 §1 permits 20 MB, so this would have blown the reclaim window on real uploads.
   `test_chunking_a_large_document_stays_linear` guards the complexity class.
4. **`db.create_engine` registered pgvector's asyncpg codec on top of the SQLAlchemy `Vector`
   type**, double-converting every vector so no chunk insert could succeed. One conversion, in
   one place.

*A fifth defect, found while reviewing contract surfaces before merge.* `chunk_count` was
only ever written on success, so a document whose **re-index failed** kept advertising the
passage count of an index that no longer existed — Instance 2's re-index transaction deletes
the chunks but writes only `status`, `progress` and `error_message` (Contract 3 §1). It is
now cleared when a run begins. `page_count` deliberately survives: it describes the file, not
the index. Contract 6 §1's `error_message` non-null *iff* `failed` invariant is now asserted
across both terminal paths.

*Two quality judgements Instance 3 will see in the UI.* A document's **title is demoted to
content** when the evidence is unambiguous, so citations read `3. Methods > 3.2 Sampling` rather
than `<Whole Paper Title> > 3. Methods > 3.2 Sampling`. And a chunk that opens before any
heading takes the first heading it contains rather than carrying no path. Heading detection
still **fails closed** — an unreliable signal turns the whole document's `heading_path` to NULL,
because a wrong heading prints under a citation as if it were fact.

*Verified against the real process, not only tests.* The worker boots, claims, ingests the
four-page two-column fixture into 18 chunks (7 running header/footer lines stripped), writes
`ready` with `progress = 1.0`, and goes idle. **The kill-the-worker check passes:** killed
mid-run, the job stayed `running` with no chunks written; after the reclaim window it was picked
up on attempt 2 and finished with exactly one set of 1200 chunks. The quality harness reports
coverage 1.00, hit@1 1.00, MRR 1.000 on the messy fixture.

*Deliberate choices a merger should not mistake for oversights.*
- **No lock heartbeat.** A killed run's job stays `running` and the 15-minute window hands it
  back. Refreshing `locked_at` would trade the contract's simple, testable recovery for a
  hung-but-alive worker that never gets reclaimed.
- **Token counts are estimates**, not Voyage BPE counts. Counting exactly means downloading a
  tokenizer at runtime, which would make chunk boundaries depend on a network fetch and break
  Contract 5 §4. `chunks.token_count` is therefore approximate by design — display metadata, and
  nothing depends on it being exact.
- **Auth failures from Voyage are treated as transient**, not permanent. A bad key is an operator
  problem; permanently failing every user's uploads over it would be worse than retrying.
- **The test suite uses its own database** (`queryll_worker_test`) on the shared container,
  applied from the frozen `db/init.sql`. The `queryll` database belongs to everyone; truncating
  its tables would have been reaching outside this lane.
- **`tests/fixtures/.gitattributes` marks the PDFs binary.** Git's autocrlf would otherwise
  rewrite them on checkout and corrupt the xref tables the determinism tests compare byte for
  byte.

*Contract surfaces to double-check at merge.* `chunks.*` and the `documents` lifecycle columns
are the only things this instance writes; `heading_path`, `page_start`/`page_end` and
`char_start`/`char_end` are what Instance 3 renders, so they are the ones worth eyeballing
against a real citation. The `ingestion_jobs` claim statement is contract text and was not
parameterised.

*ASSUMED, and unresolved by design:*
- **`input_type` has not been verified against the live Voyage API** — there is no key in this
  worktree, and Contract 4 forbids a test from calling it. The client fails loudly rather than
  silently dropping the parameter, and the ★ merge-time probe is where this gets settled.
- **Nothing here can prove this worker and the API agree about what a vector means.** That is
  the ★ cross-process cosine probe, and it is merge-time by construction.
- Chunk shape (512/64/32) and the page and size limits are the contract's ASSUMED starting
  values, unchanged; `tools/quality_harness.py --target-tokens/--overlap-tokens` is how to tune
  them against real documents.

**One amendment raised** (Contract 3 §4, above): the state machine does not say what
`documents.status` becomes when a job is requeued after a *transient* failure with
`attempts < 3`. I implemented `processing -> pending` and explained why there; it is not
user-visible, but two instances guessing differently would be. Everything else in the
frozen contracts was implementable exactly as written.

---

## INSTANCE 2 — Retrieval, Answer API & Auth  ·  STATUS: DONE

**Owns:** `backend/` in full — `app/` (models, schemas, routers, auth, documents, retrieval, the Voyage query
client, the Claude answer service, SSE streaming, citations, errors, config, database), `backend/tests/`,
`backend/requirements.txt`, `backend/pytest.ini`, `backend/Dockerfile`, `backend/.env.example`, `backend/README.md`.

**Does NOT touch:** `worker/` (anything at all), `frontend/` (anything at all), `db/init.sql`,
`docker-compose.yml`, the root `README.md`, `DEPLOY.md`, `BUILT-WITH-SWARM.md`, the INTERFACE CONTRACTS block,
or another instance's section of this file.

**Assigned skills:** `fastapi-expert`, `rag-architect`, `postgres-pro`, `api-designer`, `security-reviewer`,
`test-master` (under the `fullstack-dev-skills:` prefix), plus the standalone **`claude-api`** skill — load it
before writing a single line against the Anthropic SDK.

**Role prompt:**

You are **Instance 2**, owner of the Queryll API. You build the FastAPI service that authenticates users, accepts
uploads, retrieves the right passages, and streams a grounded, cited answer from Claude. You sit between two
instances that never talk to each other: you consume what the worker writes, and you produce everything the
frontend renders.

You **produce Contracts 1, 6, 7, 8 and 9**, you are a **peer party to Contract 3**, and you **consume Contracts 4
and 5**. Instance 3 is building the entire UI against a mock of your HTTP layer and a fake stream reader replaying
your SSE frames, without ever seeing your code. Instance 1 is writing the chunk rows you retrieve, without ever
seeing yours. If you believe a contract is wrong, write a proposed amendment in ESCALATIONS and wait for the human.

**Carry the auth layer over verbatim.** Contract 1 is LedgerLite/TaskFlow's, unchanged — argon2, HS256, httpOnly
rotating refresh cookie. Do not redesign it. Do not "modernize" it. The whole point of copying it is that the new
difficulty in this project stays concentrated on RAG.

**Retrieval is your core work.**

- The query embedding uses `input_type="query"` — Contract 4's asymmetry. Assert dimension and L2 norm on the
  returned vector and raise, exactly as Instance 1 does. You are the other half of the pair that must not diverge.
- **Order by `embedding <=> :qvec` and nothing else.** Not `<->`, not `<#>`. A mismatched operator does not error;
  it silently abandons the HNSW index for a sequential scan, and your tests on 50 fixture rows will not notice.
- **Verify the index is used, in a test**, by asserting `EXPLAIN` output contains an index scan on
  `chunks_embedding_idx` for the real retrieval query. This is the cheapest possible insurance against the single
  most common pgvector mistake.
- Enforce the per-document cap (Contract 7 §4) so one long document cannot fill the entire context and drown out a
  better passage from a shorter one.
- **Scope every retrieval query to the caller's `user_id` in SQL**, in the same `WHERE` clause as the vector search —
  not in a post-filter, not in Python. A cross-tenant leak here means one user's questions answered from another
  user's documents, with citations.
- Seed chunks with the deterministic fake embedder for tests, exactly as Instance 1 does, so the two fixture worlds
  are at least generated the same way.

**The answer path — grounded, streamed, and honest.**

- **Load the `claude-api` skill first** for current model ids, streaming and prompt-caching guidance. Use
  `claude-sonnet-5`. Cache the system prompt where the skill's guidance supports it — the instruction block is
  stable across every question and the sources are not.
- **The grounding rule (Contract 7 §5) is the product.** Below the floor, do not call Claude at all. Send the fixed
  sentence and stop. An unsourced answer in a citations app is a defect of the highest severity — one that looks
  like a feature working.
- **`retrieval` is always the first event, before the model call.** The user sees which passages are being read
  while the answer is still forming; that is most of the perceived speed of this app.
- **Validate `[n]` markers before they leave the process** (Contract 7 §7), and buffer a partial trailing marker
  across stream chunks so you never emit `[` followed a beat later by `2]`. Test with a stream deliberately split
  mid-marker.
- **Persist on `done` even if the client vanished.** A dropped reader must not cancel generation (Contract 7 §8);
  a user who refreshes should find the finished answer waiting.
- Send heartbeats every 15s, set `X-Accel-Buffering: no`, and **exclude this route from any compression
  middleware** — GZip middleware buffering is the classic way SSE dies only in production.

**Contract 3 is a discipline, not a suggestion.** You insert documents and jobs in one transaction; you never write
a worker-owned column except in the single ratified re-index exception, and only from `ready`/`failed`. If you find
yourself wanting to nudge `status` to unstick something, you have found an escalation.

**Security** (`security-reviewer` earns its place here): validate MIME by sniffing content, not by trusting the
declared type or the extension; enforce the 20 MB cap before reading the whole body into memory; 404-not-403 on
every ownership check including chunks and conversations; never serialize an embedding to a client; never log file
bytes, extracted text, keys or tokens.

**Testing:** pytest with `httpx.ASGITransport` against **real Postgres with pgvector** (`docker compose up db`) —
SQLite cannot represent a `vector` column, so there is no in-memory shortcut in this project. Stream tests assert
the exact event *sequence and ordering*, not just the final text. Prompt assembly, citation projection and marker
validation are pure functions — test them without a database or a network.

Follow the `swarm-worker` runtime protocol for all shared-file, escalation, and git rules.

**Work log:**

**2026-09-07 — Instance 2 — DONE.** `backend/` complete on branch `instance/api`. 194 tests green
against real Postgres 16 + pgvector. No test calls the live Voyage or Anthropic API; both keys are
actively unset in `conftest.py` so a stray real call fails loudly rather than quietly spending quota.

*What was built.* FastAPI + SQLAlchemy 2 (async) + asyncpg + Pydantic v2. All five produced contracts
are implemented in full: Contract 1 auth (argon2 + HS256 + rotating httpOnly refresh cookie, carried
over unchanged), Contract 6 documents (upload, list, detail, chunks, chunk-with-neighbours, re-index,
delete), Contract 7 ask (retrieval + SSE), Contract 8 citations, Contract 9 error envelope and CORS.
Every route in Contracts 1, 6 and 7 exists at the specified path and method.

*The four decisions a merger should look at first.*
1. **Generation runs in a background asyncio task feeding a queue, not in the response generator.**
   Contract 7 §8 says a client that disconnects mid-stream must still get a persisted message. Driving
   the model from inside the generator would let Starlette cancel generation on disconnect. Tested by
   walking away mid-stream and asserting the finished answer lands in the database.
2. **The work is split either side of the response headers.** Auth, ownership, query embedding,
   retrieval and the user-message write all happen before `StreamingResponse` is constructed, so a
   Voyage outage is a 503 with an envelope rather than an `error` frame inside a 200 the UI has already
   started painting. After the headers, every failure is an in-stream `error` event.
3. **There is no compression middleware anywhere in the app, deliberately.** Contract 7 §1 requires the
   ask route to be excluded from compression; having nothing to exclude it from is the version that
   cannot be reintroduced by accident. Noted in `app/main.py` and the README for whoever assembles the
   production stack.
4. **`app/services/documents.py::reindex_document` is the only function in this instance that writes a
   worker-owned column**, and only from `ready`/`failed`, all in one transaction. Uploads insert the
   `documents` row and its `ingestion_jobs` row in the same transaction (Contract 3 §2).

*Finding, from the ★ HNSW check — worth Instance 1 and the Reconciler knowing.* The index test failed
on first run, and correctly. `EXPLAIN` with only `enable_seqscan = off` showed the planner reaching the
retrieval query through `chunks_document_idx` and then **sorting** by cosine distance — no HNSW scan.
Investigated rather than assumed: the cause is cost, not an operator mismatch. On a few hundred fixture
rows, reading everything and sorting is genuinely cheaper than an index scan, and that is true even
with no filter at all. With `enable_sort = off` added, the real query — join to `documents` and all —
uses `Index Scan using chunks_embedding_idx` as the outer of a nested loop, which is the right shape.
The L2 negative control still cannot use it and falls back to a Sort priced at ~1e10. **So the query
shape is correct and the join does not block the index**, but be aware the test proves *usability*, not
that the planner picks it under production cost. ★ check 9 (EXPLAIN ANALYZE over a few thousand real
chunks at default settings) remains the one that answers that, and it is not redundant.

*Consumed contracts — what I assumed about Instance 1's output.* Chunks have contiguous 0-based
`ordinal` per document (the neighbours route walks by ordinal). `page_start`/`page_end` are 1-based
inclusive and both NULL for `.txt`/`.md`. `heading_path` is NULL rather than confidently wrong.
`error_message` is non-null exactly when `status = 'failed'` and is user-facing copy rendered verbatim.
Chunk `text` is the exact extracted slice. If any of those turn out otherwise, the citation UI is where
it will show.

*Contract surfaces to double-check at merge.* (a) The `retrieval` event's `sources` array is
`Citation` verbatim including `similarity` rounded to 3dp and `snippet` ≤ 300 chars — worth diffing
against Instance 3's hand-written `src/api/types.ts`. (b) Timestamps are ISO-8601 UTC with a literal
trailing `Z`, not `+00:00`. (c) Ids cross the wire as strings everywhere. (d) `document_ids: null` means
all documents; `[]` is read literally as zero documents and takes the `insufficient_context` path.
(e) Both `code` values on the `error` event and the live `input_type` verification are OPEN escalations
above.

*Deliberate deviations from assigned-skill defaults, all contract-driven.* `api-designer` wants RFC 7807
`application/problem+json`; Contract 9 freezes `{"error": "<sentence>"}` and the contract wins.
`rag-architect` wants hybrid BM25 + a reranker; Contract 7 §4 ratifies pure dense cosine and both would
be amendments, so neither was added — they are the obvious next quality lever once `top_k` and the floor
are tuned. `fastapi-expert` wants `X | None`; this machine has Python 3.9 only and SQLAlchemy 2 and
Pydantic v2 both evaluate annotations at runtime, so `Optional[...]` is used uniformly. Production pins
3.12 in the Dockerfile; the Anthropic SDK is imported lazily so the whole suite runs on 3.9 without it.

*One producer-side normalization worth naming.* Models emit `[1, 2]` despite instruction; the marker
filter rewrites it to `[1][2]`. Output stays exactly contract-shaped — only single `[n]` markers ever
reach the client — but it is a transformation Instance 3 did not ask for and should know about.

*Second pass, same day — three contract requirements I had implemented but never exercised.* Found by
auditing my own coverage rather than by a failure, and all three are things a merge actually hits.
(a) **The SSE heartbeat (Contract 7 §2) was untested** — every stream test used an instant scripted
client, so `: ping` never had time to fire. Now tested with a deliberately slow model and a shortened
interval, asserting the comment appears AND that it does not disturb the event sequence a client parses
out of the same stream. (b) **Multi-turn history was only unit-tested.** `build_messages` was covered
but `load_history` was not, and nothing asserted a second question actually carries the first turn to
the model, that the 6-message cap holds, that stale `[n]` markers are stripped from history, or that
prior turns' sources are not re-sent. All four now tested end to end. (c) **CORS was configured and
never asserted** — the classic silent merge-breaker, since it is browser-enforced and a mistake passes
every server-side test then fails Instance 3 with an opaque console error and a working `curl`. Now
covers preflight approval, `allow-credentials: true` (without which the refresh cookie never travels),
`Authorization` on the allow-list, the origin being echoed rather than wildcarded, and a non-allowed
origin getting nothing. Also added: the production cookie shape (Secure + SameSite=None) asserted by
flipping config, since the suite otherwise runs with the local development settings.

*ASSUMED and remaining limits* are listed in full at the end of `backend/README.md`. The load-bearing
ones: `top_k=8` / floor `0.35` / per-doc cap `4` / history `6` are all tunable config, not constants;
refresh tokens cannot be revoked server-side because Contract 2 has no token table (a schema change
would be an escalation, not an edit); duplicate signup email returns 409 and an empty upload returns
422, neither of which Contract 9 enumerates explicitly; and the prompt-cache breakpoint is declared but
almost certainly does not hit yet, since the instruction block is shorter than the minimum cacheable
prefix — verify with `usage.cache_read_input_tokens` rather than assuming it works.

---

## INSTANCE 3 — Document Library & Answer UI  ·  STATUS: DONE

**Owns:** `frontend/` in full — `src/` (auth shell, router, api client, SSE stream reader, library, uploader,
document viewer, chat, citation components, design tokens), `frontend/tests/`, `package.json`, `vite.config.ts`,
`tsconfig*.json`, `vercel.json`, `frontend/.env.example`, `frontend/README.md`.

**Does NOT touch:** `backend/` (anything at all), `worker/` (anything at all), `db/init.sql`,
`docker-compose.yml`, the root `README.md`, `DEPLOY.md`, `BUILT-WITH-SWARM.md`, the INTERFACE CONTRACTS block,
or another instance's section of this file.

**Assigned skills:** `react-expert`, `typescript-pro`, `test-master` (under the `fullstack-dev-skills:` prefix),
plus the standalone **`ui-ux-pro-max`** and **`frontend-design`** skills.

**Role prompt:**

You are **Instance 3**, owner of the Queryll frontend. You build the whole browser surface: sign-in, the document
library with upload and live ingestion status, the passage viewer, and the chat where a streamed answer assembles
itself with clickable citations.

You **consume Contracts 1, 6, 7, 8 and 9** and produce none. You will build against a **mock API client and a fake
stream reader that replays contract-shaped SSE frames on demand** — the real backend will not exist in your worktree,
and you must not wait for it. If a contract is ambiguous or missing something you need, write a proposed amendment in
ESCALATIONS and keep building around it; do not invent an endpoint.

**Build the fake stream reader first, before any chat UI.** A small utility that takes a scripted list of
`{event, data}` frames and yields them with controllable timing, wrapped in the same interface your real
`fetch` + `ReadableStream` parser exposes. Everything about the chat experience — token-by-token rendering, the
sources panel appearing before the first token, mid-stream errors, disconnection, a marker split across two
chunks — is testable through it, deterministically, and none of it is testable without it.

**The three things that make this app feel real:**

1. **Sources appear before the answer.** The `retrieval` event arrives first by contract (Contract 7 §3). Render the
   source cards the instant it lands, then stream tokens beneath them. Do not wait for `done` to show anything.
2. **Citations are the product.** `[n]` markers in the answer text render as inline chips. Hovering shows the
   `snippet`; clicking opens the passage in context via `GET /api/documents/{document_id}/chunks/{chunk_id}`, with
   its filename, heading path and page chip. Resolve by `chunk_id`, never by index or page. Dim the sources that
   `citations_used` says the answer did not actually reference.
3. **Ingestion is visible and never a mystery.** A document goes `pending → processing → ready` in front of the
   user. `processing` at `progress == 0.0` is an indeterminate state (parsing has begun, chunk count is unknown —
   Contract 3 §7); above 0.0 it is a real bar. `failed` renders `error_message` **verbatim**, with a retry button
   hitting the re-index route. Never invent your own copy for a failure the server has already explained.

**Polling discipline (Contract 6 §4).** Poll every 2s only while something is `pending` or `processing`; stop
completely when nothing is; back off to 10s after 5 minutes. **An idle library tab must issue zero network
requests** — verify it in a test with fake timers, because this is the kind of bug nobody notices until a bill or a
rate limit arrives.

**Handle the states everyone forgets.** Empty library (first-run — this is the moment that teaches someone what the
app is, so make it a real onboarding, not a shrug). A question asked with zero `ready` documents. `insufficient_context`
— render it as an honest, calm answer, not an error toast; the app working correctly and the app failing must not
look the same. A mid-stream `error` event after partial text has rendered — keep the partial text, show the error
beneath it. A 401 mid-session: refresh once, retry once, then route to login without losing the typed question.

**Auth handling:** access token in memory only (never `localStorage`), refresh via the httpOnly cookie on 401,
`credentials: 'include'` on auth routes. Upload is `multipart/form-data`; enforce the 20 MB and MIME limits
client-side too so the user gets an instant answer rather than a round-trip to a 413 — but treat the server's
rejection as authoritative regardless.

**Design:** this is a portfolio piece and the reading experience *is* the app. Aim for a calm, document-forward,
typography-led interface — generous measure and line-height in the answer column, a citation chip that is obviously
interactive without shouting, and a passage viewer that makes the source text the hero. Dark mode from day one, via
tokens. Keyboard-accessible everywhere: the uploader, the citation chips, and the passage dialog all reachable and
dismissible without a mouse.

**Testing:** Vitest + Testing Library. Every SSE behaviour through the fake stream reader. Mock the HTTP layer at
the API-client boundary, not at `fetch`, so the contract shapes live in one typed place — hand-write the contract
types in `src/api/types.ts` from this file; do not generate them from a server you cannot see.

Follow the `swarm-worker` runtime protocol for all shared-file, escalation, and git rules.

**Work log:**

- **2026-09-07 — Instance 3 — started.** Read the coordination file in full; contracts confirmed FROZEN.
  Restated domain: `frontend/` only. Consuming Contracts 1, 6, 7, 8, 9. Producing none.
  Build order set: (1) contract types hand-written from this file, (2) the fake stream reader + real
  `fetch`/`ReadableStream` SSE parser behind one interface, (3) API client + auth shell, (4) design tokens,
  (5) library + polling discipline, (6) chat + citations + passage viewer. No backend in this worktree; the
  entire UI is built against a mock API client and scripted SSE frames. — Instance 3

- **2026-09-07 — Instance 3 — DONE.** The whole browser surface is built, self-tested against the
  frozen contracts, and green: **92 tests across 8 suites**, `tsc -b` clean under `strict` +
  `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes`, production build clean (89 kB gzipped JS).

  **Where it lives:** `frontend/` — `src/api` (contracts, client, SSE), `src/auth`, `src/library`,
  `src/chat`, `src/passages`, `src/ui`, `src/dev`, `src/styles`, `tests/`, plus `vercel.json`,
  `.env.example` and `frontend/README.md`. Nothing outside `frontend/` was touched except this
  section and my own status flag.

  **Built in the order the role prompt asked for.** The fake stream reader came first, before any
  chat UI: `src/api/fakeStream.ts` ships three implementations of the one `AskStreamFn` the
  production code uses — scripted (events on a timer), manual (the test emits each one by hand), and
  raw-SSE (real bytes through the real `SseDecoder`, split at byte offsets the test chooses). The
  third is the only one that can prove a frame, or a `[n]` marker, survives being torn in half by a
  chunk boundary; that case is tested at eight different split points and at every byte.

  **Contract surfaces a merger should double-check against Instance 2's real output:**
  - **SSE frame shape (Contract 7 §2/§3).** I parse `event:`/`data:` with a spec-compliant
    incremental decoder, swallow `: ping` comments, tolerate CRLF and multi-line `data`, **ignore**
    unknown event names (forward-compatible) and **throw** on a known event with a malformed payload.
    A `done` with no `message_id` is treated as a protocol error rather than silently dropped, since
    dropping it would hang the UI forever.
  - **Event ordering.** The UI is built on `retrieval` arriving exactly once and first. A `token`
    before `retrieval` is rendered rather than discarded, but it is a contract violation and worth
    catching at merge.
  - **Error envelope (Contract 9).** Every non-2xx sentence is rendered verbatim, including
    `documents.error_message`. If any endpoint returns a bare string, a stack trace, or FastAPI's
    default `{"detail": ...}` instead of `{"error": "<sentence>"}`, the client falls back to its own
    generic sentence and the server's real explanation is lost — worth one deliberate check per
    status code (401 / 404 / 409 / 413 / 415 / 422 / 502 / 503).
  - **Upload field name and status code.** `multipart/form-data`, field `file`, expecting `202` with
    `page_count` and `chunk_count` null.
  - **Chunk-route neighbours.** The passage viewer depends on `{ chunk, prev, next }` from
    `GET /api/documents/{id}/chunks/{chunk_id}`, and on that route returning **404** (never 403) for
    a chunk whose document is gone or has been re-indexed.
  - **CORS.** The client sends `credentials: 'include'` on every route (not just the auth ones), so
    the allow-list must name the exact Vercel origin — a wildcard will fail with credentials on.

  **ASSUMED items for the human/Reconciler:**
  1. **A persisted assistant message with `citations: []` is treated as the grounding-refusal path**
     and re-rendered with the calm "no grounded answer" framing rather than as a plain answer. This
     follows from Contract 7 §5 (below the floor, `sources: []`, Claude is never called) plus the
     project rule that an answer with zero citations is otherwise a bug — but it is an inference, and
     if Instance 2 ever persists a normal answer with an empty citation array it will read wrongly.
  2. **`citations_used` is re-derived from the stored text's own markers** when a conversation is
     read back, since Contract 7 §9 persists the full `Citation` array but not the used-index list.
     Equivalent by definition (§3 defines `citations_used` as the indices actually referenced), but
     it is a derivation rather than a value read from the server.
  3. Conversation titles are mirrored locally from the first question (first 60 chars) so the rail
     does not read "Untitled" until the next load; the server stays the authority. Contract 7 §9
     already marks this ASSUMED.
  4. `display_name` fallback is display-only, per Contract 1's own ASSUMED note.

  **Things I deliberately did not do:** no endpoint was invented, no contract was edited, and no
  types were generated from a server I cannot see — `src/api/types.ts` is hand-written from the
  frozen block, with section references, and is the single typed place every test fixture flows
  through. I raised no escalations because nothing in Contracts 1, 6, 7, 8 or 9 turned out to be
  ambiguous enough to need one.

  **One extra, clearly fenced:** `src/dev/mockBackend.ts` is an in-memory `QueryllApi` behind
  `VITE_MOCK_API=1`, so the UI is demonstrable end-to-end (ingestion progression, streamed cited
  answers, the refusal path) with no API process running. It is a demo for humans, explicitly not a
  contract simulator, and no test uses it.

  **Verified locally:** `npm test` (92/92), `npm run build`, and a dev-server smoke test with the
  mock backend. Not verifiable from here and left to the merge-time ★ checks: SSE surviving the real
  deploy path (check #8) and citation truthfulness against real answers (check #7). — Instance 3

---

## MERGE-TIME ARTIFACTS & CHECKS (human / Reconciler — not assigned to any instance)

Deferred here deliberately (rule b): small, spanning all three sides, and best written once they are real.

1. **The assembled `docker-compose.yml`** — the ratified file on `main` carries only the `db` service. At merge, add
   the `api` and `worker` services so the whole system comes up with one command.
2. **`DEPLOY.md`** — the Vercel → Render → Neon runbook, extended with this project's specifics: `CREATE EXTENSION
   vector` must run on Neon before first boot; the worker deploys as a **Render Background Worker**, not a Web
   Service (no port, no health check); both services need `VOYAGE_API_KEY` and the same `EMBEDDING_MODEL`;
   the API additionally needs `ANTHROPIC_API_KEY`; and **changing `EMBEDDING_MODEL` requires re-indexing every
   document** — write that down where someone will find it.
3. **`BUILT-WITH-SWARM.md`** — the portfolio narrative: this coordination file, the frozen contracts, the
   reconciliation report. TaskFlow's is the template. This run's story is the embedding-divergence problem — "the
   one thing both processes had to do identically, and the check we built because no test could prove they did" is
   the most interesting engineering judgement here.
4. **Root `README.md`** — replace the placeholder once the app exists.
5. **★ The cross-process embedding probe** — the boundary no mock can prove. Embed one fixed probe string through
   the worker's real Voyage client and through the API's real Voyage client, and assert cosine ≥ 0.99 and identical
   dimension. Then the end-to-end version: ingest a document with the real worker, ask a question whose answer is in
   a known paragraph, and confirm that paragraph's chunk is retrieved rank 1. If the probe passes but retrieval is
   junk, the divergence is in `input_type`, not the model.
6. **★ The real-PDF ingestion check** — ingest a genuinely messy PDF (multi-column, running headers, footnotes, a
   table). Confirm headers/footers were stripped, `page_start`/`page_end` match what a human sees on the page, and
   `heading_path` is either right or `NULL` — never confidently wrong.
7. **★ The citation-truthfulness check** — ask three real questions. For each, open every citation and confirm the
   passage genuinely supports the sentence it is attached to. This is the one check that cannot be automated and the
   one that decides whether the product is honest.
8. **★ The SSE-through-the-real-deploy check** — from the deployed Vercel UI against the deployed Render API, confirm
   tokens arrive incrementally rather than in one burst at the end. If they arrive all at once, something between the
   two is buffering: check compression middleware first, then `X-Accel-Buffering`.
9. **★ The HNSW-index check** — with a few thousand real chunks loaded, `EXPLAIN ANALYZE` the production retrieval
   query and confirm an index scan on `chunks_embedding_idx`, not a sequential scan. Then confirm the ordering
   operator in the emitted SQL is `<=>`.
10. **The kill-the-worker check** — start ingesting a large document, kill the worker process mid-run, restart it,
    and confirm the job is reclaimed after the window, completes, and leaves exactly one set of chunks. This is the
    payoff for choosing a durable queue over `BackgroundTasks`; verify it once against the real thing.
11. **The cross-tenant check** — two accounts, two documents. Confirm user B cannot list, fetch, re-index, delete, or
    *retrieve over* user A's document, and that probing A's document id returns 404 rather than 403 on every route.
