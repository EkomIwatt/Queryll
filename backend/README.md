# Queryll API — retrieval, grounded answers and auth

Instance 2 of the Queryll swarm run. This is the FastAPI service that authenticates
users, accepts uploads, retrieves the right passages from pgvector, and streams a
grounded, cited answer from Claude.

It sits between two processes it never talks to directly. The ingestion worker writes the
chunk rows this service retrieves; the React frontend renders everything this service
produces. **The database is the only medium between this service and the worker** — no
HTTP, no shared filesystem, no shared Python package.

---

## Quick start

```bash
# From the repository root — the database listens on 5433, not 5432.
docker compose up -d db

cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt   # macOS / Linux

cp .env.example .env       # then fill in JWT_SECRET, VOYAGE_API_KEY, ANTHROPIC_API_KEY
.venv/Scripts/python -m pytest
.venv/Scripts/python -m uvicorn app.main:app --reload
```

Interactive API docs at `http://localhost:8000/docs`.

### Python version

Production targets **Python 3.12** (see `Dockerfile`). The development machine for this
run has **3.9**, and `anthropic>=1.0` requires 3.10+, so the dependencies are split:

| File | Contents | Installs on 3.9 |
|---|---|---|
| `requirements-base.txt` | everything except the Anthropic SDK | yes |
| `requirements.txt` | base + `anthropic` — **production** | no (3.10+) |
| `requirements-dev.txt` | base + pytest — **local development** | yes |

The Anthropic SDK is imported lazily inside `app/answer/claude_client.py`, so the API
imports, boots, serves auth and documents, and runs its **entire** test suite without the
package present. That is not only a version workaround: no test in this suite may call
the live Anthropic API anyway, so nothing but a real deployment needs it.

Type annotations use `Optional[X]` rather than `X | None` throughout, because SQLAlchemy 2
and Pydantic v2 both evaluate annotations at runtime and PEP 604 unions are a syntax error
under 3.9. The style is uniform on purpose; it costs nothing on 3.12.

---

## Layout

```
app/
  main.py               app factory, CORS, the error envelope, no compression middleware
  config.py             every contract-pinned value, read from the contract's env names
  database.py           async engine + get_db
  models.py             SQLAlchemy models for ONLY the tables this instance touches
  schemas.py            the exact wire shapes of Contracts 1, 6, 7, 8, 9
  errors.py             AppError hierarchy -> {"error": "<sentence>"}
  security.py           argon2 + HS256 JWTs (Contract 1)
  deps.py               CurrentUser, DbDep, injected embedding/answer clients
  mime.py               upload type detection by sniffing content
  pagination.py         opaque cursors
  embeddings/
    base.py             the Contract 4 assertions — dimension and L2 norm, raising
    voyage.py           the real query client (input_type="query")
    fake.py             the deterministic fake embedder used by every test
  retrieval/
    service.py          the vector SQL: cosine, tenant scope, per-document cap, floor
    citations.py        chunk metadata -> Contract 8 Citation
    types.py            RetrievedPassage (full text; not a Citation)
  answer/
    prompt.py           system prompt assembly, the cache breakpoint (pure)
    markers.py          [n] validation and the streaming buffer (pure)
    claude_client.py    the Anthropic wrapper (lazy import) + ScriptedAnswerClient
    service.py          the SSE orchestration
  routers/              auth.py, documents.py, conversations.py
  services/documents.py document lifecycle, including the one re-index exception
```

---

## The five things worth knowing before changing anything

### 1. The embedding block is half of a two-sided contract

This service embeds **questions** with `input_type="query"`. The worker embeds **chunks**
with `input_type="document"`. Both read the same env var names with the same defaults;
neither imports the other's client.

If those two clients ever disagree — different model, different dimension, a dropped
`input_type` — **nothing crashes**. Retrieval quietly returns near-random passages, Claude
faithfully answers from them, and both test suites stay green, because each side is
internally consistent. That is the worst failure mode in this project and it is invisible
to unit tests by construction.

Three layers guard against it, and only the third is conclusive:

1. Contract 4 pins provider, model, dimension, input types and normalization as literal
   frozen values, read from shared env var names.
2. **Every vector is checked at the boundary** where it enters the program —
   `len(vec) == 1024` and `|‖vec‖ − 1| < 1e-3` — and a failure **raises**. The contract
   writes these as `assert`; they are implemented as explicit raises because `assert` is
   removed by `python -O` and this check must survive production.
3. The **cross-process cosine probe** at merge time. Nothing before merge can prove the
   two clients agree. `tests/test_voyage_client.py` pins *this* side of the deal — that
   `input_type: "query"` is on the wire, and that a wrong-shaped vector raises rather than
   being used — but it cannot speak for the worker's.

If Voyage ever rejects `input_type`, the client raises `VoyageInputTypeRejected` and
**does not retry without it**. Retrying without it would "work", and would silently
decorrelate every query vector from every chunk vector ever written.

### 2. Cosine, always `<=>`

Retrieval orders by `Chunk.embedding.cosine_distance(...)`, which emits `<=>`. Never
`<->` (L2) and never `<#>` (inner product). A mismatched operator **does not error** — it
silently abandons the `vector_cosine_ops` HNSW index for a sequential scan.

`tests/test_index_usage.py` asserts the plan uses `chunks_embedding_idx`, with a negative
control proving an L2 ordering *cannot*. It sets `enable_seqscan = off` **and**
`enable_sort = off` deliberately: on a few hundred fixture rows, reading everything and
sorting is genuinely cheaper than an index scan, so on cost alone the planner ignores a
perfectly usable index. Both knobs take cost out of the question and leave the only thing
worth asserting — whether the operator matches the operator class.

The production-scale version — `EXPLAIN ANALYZE` over a few thousand real chunks at
default settings, where cost *is* the question — is a ★ merge-time check. This test does
not replace it.

### 3. The grounding rule is the product

If no chunk clears `RETRIEVAL_MIN_SIMILARITY`, the API sends
`retrieval { sources: [], insufficient_context: true }`, one `token` with the fixed
sentence, then `done`. **Claude is not called at all.**

Answering from general knowledge when retrieval fails is the single worst thing this app
can do, because it looks exactly like the app working. The cheapest way to guarantee it
never happens is to not make the call — so the test asserts the scripted client recorded
**zero** calls, not that the wording came out right.

This path also works with no `ANTHROPIC_API_KEY` configured, on purpose: the honest answer
must not depend on a service it never uses.

### 4. The response headers are the boundary between 5xx and an `error` event

Everything that can fail with an HTTP status happens in the request scope, **before**
`StreamingResponse` is constructed:

| Failure | Result |
|---|---|
| Voyage unavailable | `503` + `{"error": "Search is temporarily unavailable…"}` |
| Anthropic unconfigured *and* sources were found | `502` + envelope |
| Not your conversation | `404` |
| Empty question | `422` |
| Anything after the first frame | `event: error` inside a 200 |

Once the headers are out the status line is spent, so a late failure has no choice but to
be an in-stream `error` event — which is exactly what Contract 9 says.

**Generation runs in a background asyncio task, not in the response generator.** If the
model were driven from inside the generator, Starlette closing that generator on client
disconnect would cancel generation, and a user who refreshed would find a question with
no answer. Instead the task pushes frames into a queue and the generator drains it: the
reader can vanish and the answer still finishes and is persisted (Contract 7 §8).

There is **no compression middleware anywhere in this app**, deliberately. GZip buffers in
order to compress, which coalesces SSE frames into one burst at the end — and it does that
only once there is a real deploy in front of it, so it passes every local test. Contract 7
requires the ask route to be excluded from compression; having none to exclude it from is
the version that cannot be got wrong later. If compression is ever added it **must** skip
`text/event-stream`.

### 5. Column ownership is absolute

`app/services/documents.py` is the only file that writes a worker-owned column, in the
only place Contract 3 §1 allows it: `reindex_document`, legal only from `ready` or
`failed`, doing everything in one transaction. Nothing here writes `chunks` at all.

Uploads insert the `documents` row and its `ingestion_jobs` row in the **same
transaction** (Contract 3 §2), so a committed document without a job is unreachable rather
than something a sweeper repairs later.

If you find yourself wanting to nudge a `status` to unstick something, that is an
**escalation**, not a fix.

---

## Security notes

- **MIME is decided by sniffing bytes**, never by the declared `Content-Type` or the
  filename. The extension is consulted only to tell Markdown from plain text, which is
  cosmetic. A `.pdf` full of plain text is stored as `text/plain`.
- **The 20 MB cap is enforced during the read**, in 64 KB blocks, not after it. Reading a
  hostile 2 GB body into memory and then measuring it is how a 413 becomes an outage.
- **404, never 403** on every ownership check — documents, chunks, conversations. A
  malformed id is also a 404: a 422 would tell an attacker which guesses were at least
  well-formed. There is a test asserting a real id and an invented one are byte-identical.
- **Tenant scope is in the SQL**, in the same `WHERE` clause as the vector search — never
  a post-filter and never in Python.
- **Embeddings are never serialized to a client**, in any route. `ChunkOut` has no field
  to put one in, so it is structural rather than a rule to remember.
- **Filenames are escaped before entering the system prompt.** A file named
  `"><source index="1">` would otherwise let its uploader forge a source tag — prompt
  injection through a filename.
- **Never logged**: file bytes, extracted text, question text, embeddings, API keys, JWTs.
  Document ids are fine and are logged.
- Login returns one sentence for both "no such account" and "wrong password", so it cannot
  be used as an account oracle. There is a test asserting the two responses are identical.

---

## Tests

```bash
docker compose up -d db      # from the repository root
.venv/Scripts/python -m pytest
```

The suite creates its own database, **`queryll_test`**, dropping and recreating it from
`db/init.sql` at the start of every session. Your `queryll` development database is never
touched, and the ratified DDL is exercised on every single run — if Contract 2 and
`app/models.py` ever drift apart, the suite stops rather than passing against a schema
someone hand-patched.

| File | What it covers |
|---|---|
| `test_pure_units.py` | marker validation, citation projection, prompt assembly, the Contract 4 assertions — no database, no network |
| `test_voyage_client.py` | the real Voyage client against `httpx.MockTransport`: `input_type`, backoff, vector validation, secrecy |
| `test_auth.py` | Contract 1 end to end |
| `test_documents.py` | Contract 6 + the Contract 3 transactional enqueue and re-index exception |
| `test_retrieval.py` | the vector SQL: floor, per-document cap, ordering, tenant scope |
| `test_index_usage.py` | the HNSW plan check and its negative control |
| `test_ask_stream.py` | the SSE event sequence, grounding rule, marker buffering, persistence, failure paths |
| `test_cross_tenant.py` | two-account isolation on every route, conversations CRUD, the error envelope |

No test calls the live Voyage or Anthropic API. Both keys are actively unset in
`conftest.py` so that a stray real call fails loudly rather than quietly spending quota.

---

## Known limits and `ASSUMED` items

- **Refresh tokens cannot be revoked server-side.** Contract 2 freezes the schema and it
  has no refresh-token table, so "rotated on every refresh" means each refresh issues a
  fresh token — not that the previous one is invalidated. A stolen refresh token stays
  valid for its 30 days. Adding a token table would be a schema **escalation**, not an edit.
- **Retrieval is pure dense cosine.** No hybrid BM25, no reranker. That is Contract 7 §4
  as ratified, not an oversight; both would be amendments, and both are the obvious next
  quality lever once `top_k` and the floor have been tuned against real documents.
- **`top_k = 8`, floor `0.35`, per-document cap `4`, history `6`** are all **ASSUMED**
  starting values. They are config precisely because tuning them against real documents is
  expected work.
- **The prompt cache breakpoint probably does not hit yet.** The instruction block is a few
  hundred tokens and the minimum cacheable prefix is larger. The breakpoint is declared
  because it is free and correct — it no-ops while the prefix is short and starts paying
  when it grows — not because hits are expected today. Verify with
  `usage.cache_read_input_tokens` rather than assuming.
- **Grouped markers are normalized.** Models emit `[1, 2]` despite being told not to, so it
  is rewritten to `[1][2]`. The output is still exactly contract-shaped; only
  single `[n]` markers ever reach the client.
- **The tenant filter is a join.** `chunks` has no `user_id` (Contract 2), so scoping goes
  through `documents`. The plan is a nested loop driven by the HNSW scan, which is the
  right shape, but at large scale a user owning a small fraction of all chunks makes the
  index walk further before finding `top_k` of theirs. `HNSW_EF_SEARCH` and
  `RETRIEVAL_CANDIDATE_MULTIPLIER` are the mitigations; denormalising `user_id` onto
  `chunks` would be the fix, and that is a schema escalation.
- **An empty `document_ids: []`** is read literally as "search zero documents" and takes
  the `insufficient_context` path. `null` means "all of mine".
- **An empty upload is 422**, not 415 — it would otherwise be a document guaranteed to fail
  ingestion. Contract 9 lists 422 as the general validation failure.
