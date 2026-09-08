# Deploying Queryll

**Vercel (UI) → Render Web Service (API) + Render Background Worker (ingestion) → Neon (Postgres).**

Four processes, one database, and exactly one thing that will break silently if you get it wrong.
Read that part first.

---

## The one that fails silently

Queryll embeds text in **two separately written programs**: the worker embeds document chunks, the
API embeds questions. They never call each other. A vector is only comparable to another vector if
both sides used the **identical provider, model id, dimension and normalization**.

Get that wrong on one side and **nothing crashes**. There is no error, no failed health check, no
red line in a log. Retrieval quietly returns near-random passages, Claude faithfully answers from
them with citations attached, and every test on both sides stays green — because each side is
internally consistent with itself.

So:

- `VOYAGE_API_KEY`, `EMBEDDING_MODEL` and `EMBEDDING_DIM` go on **both** the API service and the
  worker service, with **identical values**. Set them in one place and forget the other and you
  have shipped the failure above.
- **Changing `EMBEDDING_MODEL` requires re-indexing every document.** Old vectors were produced by
  a different model and are not comparable to new query vectors. Change the variable on both
  services, redeploy both, then re-index every document (`POST /api/documents/{id}/reindex`). Until
  that finishes, answers are drawn from a mixture of two vector spaces, which is worse than either.
- **Changing `EMBEDDING_DIM` is a schema change**, not a config change — `chunks.embedding` is
  `vector(1024)`. It requires a ratified amendment to Contract 2, not an edit.

After any deploy that touches embeddings, run the cross-process probe in
[§7 Verifying a deploy](#7-verifying-a-deploy). It takes a minute and it is the only thing that
actually proves the two processes agree.

---

## 1. Neon (Postgres 16 + pgvector)

1. Create a Neon project. Note the connection string.
2. **Enable the extensions before first boot.** Neon does not create them for you, and the API and
   worker will both fail to start without them:

   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   CREATE EXTENSION IF NOT EXISTS pgcrypto;
   ```

3. Apply the schema. `db/init.sql` is the single source of truth — there is no Alembic in this
   project and no `create_all` against a real database:

   ```bash
   psql "$NEON_URL" -f db/init.sql
   ```

4. Convert the URL for SQLAlchemy's async driver. Neon hands you `postgresql://...`; both Python
   services need **`postgresql+asyncpg://...`**, and asyncpg does not accept `?sslmode=require` as
   a query parameter — drop it (asyncpg negotiates TLS on its own):

   ```
   DATABASE_URL=postgresql+asyncpg://user:pass@ep-xxx.region.aws.neon.tech/queryll
   ```

The HNSW index in `init.sql` is created on an empty table, which is correct and fast. It fills as
chunks are written.

## 2. Render — the API (a **Web Service**)

- **Root directory:** `backend` · **Environment:** Docker (uses `backend/Dockerfile`)
- **Health check path:** `/api/health`

| Variable | Value |
|---|---|
| `DATABASE_URL` | the `postgresql+asyncpg://...` Neon URL |
| `JWT_SECRET` | generate with `secrets.token_urlsafe(48)` |
| `COOKIE_SECURE` | `true` |
| `COOKIE_SAMESITE` | `none` — Vercel → Render is cross-site; the refresh cookie will not travel otherwise |
| `CORS_ORIGINS` | the Vercel production origin **and** the preview origins, comma-separated |
| `VOYAGE_API_KEY` | **same value as the worker** |
| `EMBEDDING_MODEL` | `voyage-4` — **same value as the worker** |
| `EMBEDDING_DIM` | `1024` — **same value as the worker** |
| `ANTHROPIC_API_KEY` | API only; the worker never calls Claude |
| `ANSWER_MODEL` | `claude-sonnet-5` |

**Do not put a compression or buffering proxy in front of `/api/conversations/{id}/ask`.** GZip
middleware coalesces SSE frames and is the classic way streaming works locally and dies in
production. The app ships with **no compression middleware anywhere**, deliberately — there is
nothing to exclude the route from, so it cannot be reintroduced by accident. It sets
`X-Accel-Buffering: no` and `Cache-Control: no-cache` on that route and sends a `: ping` comment
every 15 seconds so an idle proxy does not close the connection.

## 3. Render — the ingestion worker (a **Background Worker**)

- **Root directory:** `worker` · **Environment:** Docker (uses `worker/Dockerfile`)
- **Service type: Background Worker, not Web Service.** It binds no port and answers no health
  check. Deploying it as a Web Service makes Render wait for a port that never opens and then kill
  it — the symptom is documents that sit at `pending` forever.

| Variable | Value |
|---|---|
| `DATABASE_URL` | the same Neon URL as the API |
| `VOYAGE_API_KEY` | **same value as the API** |
| `EMBEDDING_MODEL` | `voyage-4` — **same value as the API** |
| `EMBEDDING_DIM` | `1024` — **same value as the API** |
| `EMBEDDING_PROVIDER` | `voyage`. **Never `fake` in production** — the fake embedder produces noise, and retrieval over a fake-embedded library is meaningless |

Run **one** worker. The claim query is correct under N workers (`FOR UPDATE SKIP LOCKED`, proven by
a concurrency test), so scaling later is a config change rather than a rewrite — but one is enough
for this workload, and a second buys nothing until ingestion is the bottleneck.

A killed worker leaves its job `running`; the 15-minute reclaim window in Contract 3 §3 hands it
back on the next poll. That delay is by design — ingestion is idempotent, so a reclaimed job
rewrites its chunks rather than duplicating them.

## 4. Vercel — the frontend

- **Root directory:** `frontend` · Vite preset · `frontend/vercel.json` handles SPA rewrites.
- `VITE_API_BASE_URL` = the Render API origin, **no trailing slash**.

Then go back and add the deployed Vercel origin — production **and** preview — to `CORS_ORIGINS`
on the API and redeploy it. Credentials are allowed on every route, so a wildcard origin is not a
legal option: it must be the exact origins.

## 5. Order of operations

1. Neon: extensions, then `db/init.sql`.
2. Worker (Background Worker) — so nothing sits `pending` the moment uploads start working.
3. API (Web Service).
4. Vercel, pointed at the API.
5. Add the Vercel origins to `CORS_ORIGINS`; redeploy the API.
6. Run §7.

## 6. Local — the whole system in one command

```bash
export VOYAGE_API_KEY=...  ANTHROPIC_API_KEY=...
docker compose up --build            # db + api + worker
cd frontend && npm install && npm run dev
```

`docker compose up -d db` alone starts just the database, which is what the three instances
developed against. The database is on **host port 5433**, not 5432.

The UI also runs with no backend at all — `VITE_MOCK_API=1 npm run dev` serves an in-memory mock
so the interface is demonstrable without keys. It is a demo for humans, not a contract simulator.

## 7. Verifying a deploy

Do these in order. The first is the one that matters.

1. **★ The cross-process embedding probe.** Embed one fixed string through the worker's real client
   and through the API's real client; assert **cosine ≥ 0.99** and identical dimension. This is the
   only check that proves the two processes agree about what a vector means, and no test in either
   suite can do it — by construction, since neither may call the live API.
2. **★ End-to-end retrieval.** Upload a document, wait for `ready`, and ask a question whose answer
   is in a known paragraph. Confirm that paragraph's chunk comes back **rank 1**. If the probe in
   (1) passed but retrieval is junk, the divergence is in `input_type`, not the model — the worker
   must send `"document"` and the API `"query"`.
3. **★ SSE actually streams.** From the deployed UI, ask a question and watch the answer. Tokens
   must arrive **incrementally**. If they all land at once at the end, something between Vercel and
   Render is buffering: check compression middleware first, then `X-Accel-Buffering`.
4. **★ The HNSW index is used.** With a few thousand real chunks loaded, `EXPLAIN ANALYZE` the
   retrieval query and confirm an `Index Scan using chunks_embedding_idx` — not a sequential scan —
   and that the ordering operator is `<=>`. pgvector silently falls back to a sequential scan when
   the operator does not match the index's operator class; it does not error.
5. **The kill-the-worker check.** Start ingesting a large document, kill the worker mid-run, restart
   it. The job should be reclaimed after the window, complete, and leave **exactly one** set of chunks.
6. **The cross-tenant check.** Two accounts, two documents. Confirm user B cannot list, fetch,
   re-index, delete or *retrieve over* user A's document, and that probing A's ids returns **404,
   never 403**, on every route.

## 8. Operational notes

- **Free-tier Render spins down.** The first request after idle is slow, and the worker sleeping is
  why a document can sit at `pending` for a minute. This is the exact failure `BackgroundTasks`
  would have made permanent and a durable job row makes survivable.
- **A stuck document** is a row, so it is inspectable:
  `SELECT state, attempts, last_error FROM ingestion_jobs WHERE document_id = '...';`
  `attempts >= 3` means it is permanently `failed`, and `documents.error_message` holds the
  user-facing sentence explaining why.
- **Never log** file bytes, extracted document text, embeddings, API keys or JWTs. Document and
  chunk ids are fine. This holds for `documents.error_message` too — it is rendered verbatim in the
  UI, so it is user-facing copy, never a stack trace.
- **Re-indexing** is the answer to a chunking change, an embedding-model change, or a parser fix.
  Chunk **ids change** on a re-index, which is why citations are resolved at answer time and never
  cached across one — a historical answer's citations will 404 and render as "source no longer
  available", which is intended behaviour rather than a bug.
