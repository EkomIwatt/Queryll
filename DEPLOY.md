# Running and deploying Queryll

**The real system runs locally with one command. Only the UI is deployed.**

Queryll is three processes and a database. Two of them — the API and the ingestion worker — need
long-lived processes, which is the one thing free hosting tiers do not give you (see
[§5 Hosting](#5-hosting-what-works-and-what-cannot-work)). So the default setup is:

| Piece | Where | Cost |
|---|---|---|
| Postgres + pgvector | local Docker | free |
| Ingestion worker | local Docker | free |
| API | local Docker | free |
| Frontend (public link) | Vercel, mock backend | free |
| Embeddings (Voyage) | 200M free tokens per account | free |
| Answers (Anthropic) | prepaid credits, ~$0.015/question | ~$5 goes a long way |

Read [§1](#1-the-one-that-fails-silently) before anything else — it is the one misconfiguration
that produces no error at all.

---

## 1. The one that fails silently

Queryll embeds text in **two separately written programs**: the worker embeds document chunks, the
API embeds questions. They never call each other. A vector is only comparable to another vector if
both sides used the **identical provider, model id, dimension and normalization**.

Get that wrong on one side and **nothing crashes**. There is no error, no failed health check, no
red line in a log. Retrieval quietly returns near-random passages, Claude faithfully answers from
them with citations attached, and every test on both sides stays green — because each side is
internally consistent with itself.

This already happened once, at merge, in the *test doubles*: the two instances built different
deterministic fake embedders, both 1024-dimensional and both unit-normalized, and the integrated
system answered "I could not find anything about that in your documents" to every question with all
441 tests passing. See `BUILT-WITH-SWARM.md`.

So:

- `VOYAGE_API_KEY`, `EMBEDDING_MODEL` and `EMBEDDING_DIM` must be **identical** for the API and the
  worker. `docker compose` reads both from the same variables, which is why the compose file is the
  safe way to run this.
- **Changing `EMBEDDING_MODEL` requires re-indexing every document.** Old vectors came from a
  different model and are not comparable to new query vectors. Until the re-index finishes, answers
  are drawn from a mixture of two vector spaces, which is worse than either.
- **Changing `EMBEDDING_DIM` is a schema change**, not a config change — `chunks.embedding` is
  `vector(1024)`. It requires a ratified amendment to Contract 2, not an edit.

## 2. Running the real system

One command brings up Postgres, the API and the ingestion worker:

```bash
export VOYAGE_API_KEY=...  ANTHROPIC_API_KEY=...
docker compose up --build
```

Then the UI:

```bash
cd frontend && npm install && npm run dev      # http://localhost:5173
```

`frontend/.env` needs `VITE_API_BASE_URL=http://localhost:8000` (no trailing slash).

`db/init.sql` runs automatically the first time the volume is empty — it creates the `vector` and
`pgcrypto` extensions and the whole schema, so there is no separate migration step. There is no
Alembic in this project; `init.sql` is the single source of truth. To re-apply it after a ratified
schema amendment: `docker compose down -v && docker compose up --build`.

The database is on **host port 5433**, not 5432 — Snipp already holds 5432 on this machine.

`docker compose up -d db` alone starts just the database, which is what the three instances
developed against and what the test suites need.

### Where the keys come from

- **Voyage** — dash.voyageai.com. 200M free tokens per account; ingestion and query embedding both
  fit inside that comfortably.
- **Anthropic** — console.anthropic.com → Billing → add credits, then Get API Keys. Prepaid and
  **separate from any Claude.ai subscription** — Pro/Max does not include API credits. Sonnet 5 is
  $2/$10 per MTok, which works out to roughly **$0.015 per question** (~5.5K input from the system
  prompt plus 8 retrieved chunks plus history, ~400 output). `ANSWER_MODEL=claude-haiku-4-5` roughly
  halves that with little quality loss on grounded extraction, since the hard work here is retrieval
  rather than reasoning.

Two things keep the bill down without any effort: retrieval below the similarity floor **never calls
Claude at all** (Contract 7 §5), and history is capped at 6 messages rather than resending whole
conversations.

## 3. The public link — frontend only

Instance 3 shipped an in-memory mock backend, so the entire UI is demonstrable with **no API
process and no keys**: uploads progress through ingestion, answers stream, citations open.

Vercel:

- **Root Directory:** `frontend` · Framework: Vite (`frontend/vercel.json` handles SPA rewrites)
- **Environment:** `VITE_MOCK_API=1`, and leave `VITE_API_BASE_URL` unset

That gives a link anyone can open, always up, costing nothing. It is a demo for humans, explicitly
**not** a contract simulator — no test uses it, and it proves nothing about the backend.

Run it locally the same way: `VITE_MOCK_API=1 npm run dev`.

## 4. Full local env reference

`docker compose` supplies sane defaults for everything below; these are the variables worth knowing.

**Both Python services (must match — see §1):**

```
VOYAGE_API_KEY=<your key>
EMBEDDING_MODEL=voyage-4
EMBEDDING_DIM=1024
```

**API only:**

```
ANTHROPIC_API_KEY=<your key>
ANSWER_MODEL=claude-sonnet-5        # or claude-haiku-4-5 to halve the cost
JWT_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(48))">
RETRIEVAL_TOP_K=8                   # ASSUMED starting values (Contract 7 §4),
RETRIEVAL_MIN_SIMILARITY=0.35       # tunable config rather than constants
RETRIEVAL_PER_DOCUMENT_CAP=4
```

**Worker only:**

```
EMBEDDING_PROVIDER=voyage           # never `fake` outside tests — it produces noise
CHUNK_TARGET_TOKENS=512             # Contract 5 §2; re-index after changing
CHUNK_OVERLAP_TOKENS=64
```

Cookie settings only matter when the browser and API are on different sites. Locally they are
same-site over plain http, so `COOKIE_SECURE=false` / `COOKIE_SAMESITE=lax` (what compose sets). A
`Secure` cookie is not stored over `http://`, so the production defaults break local login.

## 5. Hosting — what works, and what cannot work

If you later want the real stack publicly reachable, the constraint to design around is that
**the API cannot run on serverless.** Contract 7 §8 requires generation to survive the client
disconnecting: the answer is driven by a background asyncio task that outlives the HTTP response, so
a refresh finds the finished answer waiting. Serverless functions are killed when the response
closes. That is an architectural incompatibility, not a configuration problem — Vercel Functions,
Cloudflare Workers and Lambda are all out for this service.

What the two backend processes need is ordinary long-lived containers:

- **Fly.io** — Docker, long-lived processes, a free allowance. Closest fit; runs both services.
- **Render** — works, but note the **Background Worker type is not on the free tier** (~$7/month).
  Free Web Services exist but spin down after ~15 minutes idle, with a ~50s cold start.
- **Neon** — managed Postgres if you want the database off your machine. Enable `vector` and
  `pgcrypto`, apply `db/init.sql`, and give both services the same URL. Note the two URL forms:
  `psql` takes Neon's string as given (keep `?sslmode=require`), while the Python services need
  `postgresql+asyncpg://...` with `sslmode` stripped — asyncpg rejects it as a query parameter and
  negotiates TLS itself.

**The worker can run anywhere that reaches the database.** It shares nothing with the API but
Postgres — no volume, no bucket, no shared filesystem (ratified decision #4). So it is entirely
reasonable to host the API and run the worker on your own machine only when you need to ingest:
documents queue as durable rows and are picked up whenever the worker next starts. That is the
durable-queue choice paying off.

If you do deploy the API behind a proxy, **do not put compression or buffering in front of
`/api/conversations/{id}/ask`.** GZip middleware coalesces SSE frames and is the classic way
streaming works locally and dies in production. The app ships with **no compression middleware
anywhere**, deliberately — there is nothing to exclude the route from, so it cannot be reintroduced
by accident. It sets `X-Accel-Buffering: no` and `Cache-Control: no-cache` and heartbeats every 15s.
CORS also has to name the exact frontend origin: credentials are allowed on every route, so a
wildcard is not a legal option.

## 6. Verifying it works

Do these in order. The first is the one that matters.

1. **★ The cross-process embedding probe.** Embed one fixed string through the worker's real client
   and through the API's real client; assert **cosine ≥ 0.99** and identical dimension. This is the
   only check that proves the two processes agree about what a vector means, and no test in either
   suite can do it — by construction, since neither may call the live API.
2. **★ End-to-end retrieval.** Upload a document, wait for `ready`, and ask a question whose answer
   is in a known paragraph. Confirm that paragraph's chunk comes back **rank 1**. If the probe in
   (1) passed but retrieval is junk, the divergence is in `input_type`, not the model — the worker
   must send `"document"` and the API `"query"`.
3. **★ SSE actually streams.** Ask a question and watch the answer. Tokens must arrive
   **incrementally**. If they all land at once at the end, something is buffering: check compression
   middleware first, then `X-Accel-Buffering`.
4. **★ The HNSW index is used.** With a few thousand real chunks loaded, `EXPLAIN ANALYZE` the
   retrieval query and confirm an `Index Scan using chunks_embedding_idx` — not a sequential scan —
   and that the ordering operator is `<=>`. pgvector silently falls back to a sequential scan when
   the operator does not match the index's operator class; it does not error.
5. **★ Real-PDF ingestion.** Ingest a genuinely messy PDF — multi-column, running headers, footnotes,
   a table. Confirm headers and footers were stripped, `page_start`/`page_end` match what a human
   sees on the page, and `heading_path` is either right or `NULL` — never confidently wrong.
6. **★ Citation truthfulness.** Ask three real questions. For each, open every citation and confirm
   the passage genuinely supports the sentence it is attached to. This is the one check that cannot
   be automated and the one that decides whether the product is honest.
7. **The kill-the-worker check.** Start ingesting a large document, kill the worker mid-run, restart
   it. The job should be reclaimed after the 15-minute window, complete, and leave **exactly one**
   set of chunks.
8. **The cross-tenant check.** Two accounts, two documents. Confirm user B cannot list, fetch,
   re-index, delete or *retrieve over* user A's document, and that probing A's ids returns **404,
   never 403**, on every route.

## 7. Operational notes

- **A stuck document** is a row, so it is inspectable:
  `SELECT state, attempts, last_error FROM ingestion_jobs WHERE document_id = '...';`
  `attempts >= 3` means it is permanently `failed`, and `documents.error_message` holds the
  user-facing sentence explaining why.
- **Documents sitting at `pending` forever** means no worker is running. That is a normal, recoverable
  state rather than data loss — the job is a durable row, and starting the worker drains the queue.
  This is exactly the failure `BackgroundTasks` would have made permanent.
- **Never log** file bytes, extracted document text, embeddings, API keys or JWTs. Document and
  chunk ids are fine. This holds for `documents.error_message` too — it is rendered verbatim in the
  UI, so it is user-facing copy, never a stack trace.
- **Re-indexing** is the answer to a chunking change, an embedding-model change, or a parser fix.
  Chunk **ids change** on a re-index, which is why citations are resolved at answer time and never
  cached across one — a historical answer's citations will 404 and render as "source no longer
  available", which is intended behaviour rather than a bug.
