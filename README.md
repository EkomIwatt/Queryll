# Queryll

Ask questions about your own documents and get an answer that is **grounded in them and cites the
exact passages it came from**.

Upload a PDF or a text file, watch it get parsed, chunked and embedded in the background, then ask
a question in plain language. The answer streams back token by token with numbered citations;
clicking one opens the source passage in context, with its page number. When your documents do not
contain the answer, Queryll says so instead of guessing.

---

## What it does

- **Structure-aware chunking.** Documents are split at headings, paragraphs and sentence
  boundaries — not every 1000 characters — so a passage still reads as a passage when it appears
  under a citation. PDF running headers and footers are stripped before chunking, because otherwise
  they appear in every chunk and drag every embedding toward the same boilerplate.
- **Vector retrieval in Postgres.** Chunks and their embeddings are written in one transaction to
  one database, and retrieval is ordinary SQL over an HNSW index with cosine distance. A chunk and
  its vector cannot drift apart because they were never in two places.
- **Grounded answers, or none.** If no retrieved passage clears the similarity floor, Claude is
  **not called at all** — Queryll answers "I could not find anything about that in your documents."
  Answering from general knowledge when retrieval fails is the worst thing a citations app can do,
  and the cheapest way to guarantee it never happens is to not make the call.
- **Citations that resolve.** Every `[n]` marker is validated against the sources actually sent
  before it leaves the server, and resolves by stable chunk id — never by page, ordinal or rank.
- **Ingestion you can watch.** A 40-second embedding job runs in a separate process, survives a
  restart, and reports real progress the whole time.

## Architecture

Three processes and a database. The API and the worker **share nothing but Postgres** — no queue
broker, no shared filesystem, no shared code.

```
  Browser (React + Vite)
      |  HTTPS + SSE over fetch/ReadableStream
      v
  API (FastAPI)  ------ writes documents + job rows ------.
      |  retrieval SQL (cosine <=>)                       |
      v                                                   v
  Postgres 16 + pgvector  <---- chunks + vectors ----  Ingestion worker
                                                       (standalone Python,
                                                        polls a job table)
```

- **Queue:** a Postgres table claimed with `SELECT ... FOR UPDATE SKIP LOCKED`. No Redis, no Celery.
  A job is a durable row, so a document survives the API process restarting mid-upload.
- **Embeddings:** Voyage `voyage-4`, 1024 dimensions, L2-normalized. `input_type="document"` for
  chunks, `"query"` for questions — the asymmetry matters.
- **Answers:** Claude `claude-sonnet-5`, streamed as SSE frames over a POST.
- **Files:** stored as `bytea` in Postgres, so the two services need no shared disk.

| Layer | Stack |
|---|---|
| Frontend | React 19 · Vite · TypeScript · react-router |
| API | FastAPI · SQLAlchemy 2 (async) · asyncpg · Pydantic v2 |
| Worker | Standalone Python — no web framework, no broker |
| Database | Postgres 16 + pgvector (HNSW, `vector_cosine_ops`) |
| Auth | argon2 · HS256 JWT access token · httpOnly rotating refresh cookie |
| Tests | pytest against real Postgres/pgvector · Vitest + Testing Library |

## Running it

```bash
export VOYAGE_API_KEY=...  ANTHROPIC_API_KEY=...
docker compose up --build          # database + API + worker

cd frontend && npm install && npm run dev
```

The database is on **host port 5433**, not 5432. `docker compose up -d db` starts just the
database. To see the interface with no backend and no API keys at all:

```bash
cd frontend && VITE_MOCK_API=1 npm run dev
```

Full deployment instructions — Vercel, Render, Neon, and the one misconfiguration that fails
silently — are in **[DEPLOY.md](DEPLOY.md)**.

## Tests

```bash
cd worker   && pytest      # 155 — parsing, chunking, offsets, queue, crash recovery
cd backend  && pytest      # 194 — auth, retrieval, SSE ordering, citations, cross-tenant
cd frontend && npm test    #  92 — stream reader, citations, polling discipline
```

Anything touching SQL runs against a real Postgres with pgvector; there is no SQLite fallback,
because SQLite cannot represent a `vector` column. No test calls the live Voyage or Anthropic API.

## How it was built

Queryll was built by **three Claude Code instances working in parallel**, in separate git
worktrees, against interface contracts frozen before any of them started — and none of them ever
saw another's code. The story of that run, including the failure mode that was invisible to all
441 tests, is in **[BUILT-WITH-SWARM.md](BUILT-WITH-SWARM.md)**.
