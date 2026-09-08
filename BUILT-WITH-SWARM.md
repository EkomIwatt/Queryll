# Built with Swarm

Queryll was built by **three Claude Code instances working in parallel**, in three separate git
worktrees, against interface contracts frozen and committed to `main` before any of them started.
No instance ever saw another's code. They were merged at the end by a fourth instance — the
Reconciler — whose job was to prove that every frozen contract actually held between each
producer's real implementation and each consumer's real code.

This document is the record of that run: how the work was split, what the contracts had to pin
down, and the one defect that all 441 tests were structurally incapable of catching.

---

## The split

| Instance | Owns | Produces | Consumes |
|---|---|---|---|
| **1 — Ingestion worker** | `worker/` | Contract 5 (chunking) | Contracts 2, 3, 4 |
| **2 — Retrieval & answer API** | `backend/` | Contracts 1, 6, 7, 8, 9 | Contracts 2, 3, 4, 5 |
| **3 — Library & answer UI** | `frontend/` | — | Contracts 1, 6, 7, 8, 9 |

The seams are two process boundaries: worker vs. API vs. browser.

The frontend split was never in question — different language, disjoint directory, one HTTP
surface. The real argument was whether **ingestion and retrieval are one backend or two**. They
are both Python, both talk to the same database, and both call the same embeddings API.

The split held because the two are **two separately deployed processes** that communicate through
exactly one medium — rows in Postgres — and that medium is fully specifiable in text: the DDL and
a document lifecycle state machine. Both directions stub cleanly. Instance 2 tested retrieval by
seeding chunk rows with fixture vectors; the worker never had to run. Instance 1 tested ingestion
by inserting a document row and a job row itself; the API never had to exist.

And each side was substantial independent work. Instance 1: PDF parsing, structure-aware chunking,
embedding batching, rate-limit backoff, idempotent re-runs, crash recovery. Instance 2: auth,
upload, retrieval SQL and index tuning, prompt assembly, citation projection, SSE streaming, and
the grounding-refusal path.

## What had to be frozen

Nine contracts, ratified and committed to `main` **before the worktrees were created**, so all
three instances branched from byte-identical text and byte-identical DDL.

The load-bearing ones were not the HTTP routes. They were:

- **Contract 2 — the schema.** Owned by *no instance* after ratification. Both Python instances
  declare SQLAlchemy models for only the tables they touch, mapping the same frozen DDL. No
  Alembic, no `create_all`. A schema change was defined as an escalation, not an edit.
- **Contract 3 — the document lifecycle.** The entire interface between two processes that never
  call each other. Its core is a **column ownership table**: every mutable column has exactly one
  writer. Reading another instance's column is fine; writing it is a violation even when it would
  obviously work.
- **Contract 4 — the embedding specification.** See below. This is the one that mattered.

## The problem this project was really about

The dangerous version of Queryll is the one where **the embedding call lives on both sides with no
agreement between them**.

It genuinely does live on both sides. The worker embeds chunks; the API embeds questions. That is
unavoidable — moving query-embedding into the worker would put a queue round-trip in the middle of
a user's question.

Vectors are only comparable if both sides use the identical provider, model id, dimension and
normalization. Get that wrong on one side and **nothing crashes**. Retrieval quietly returns
near-random passages, Claude faithfully answers from them with citations attached, and **both test
suites stay green**, because each side is internally consistent with itself.

That is the worst failure mode available to this product, and it is invisible to unit tests *by
construction*.

The plan defused it in three layers:

1. **Contract 4 pins the literal values** — provider, model id, dimension, both input types,
   normalization — read from the same env var names with the same defaults on both sides.
2. **Both sides assert at runtime**, not in a comment, that every vector has `len(vec) == 1024` and
   `norm(vec) ≈ 1.0`, and fail loudly rather than store or query with a wrong-shaped vector.
   Both instances independently implemented these as explicit `raise` statements rather than
   `assert`, on the grounds that `python -O` strips `assert` — a judgement neither could have
   coordinated with the other.
3. **A cross-process cosine probe is a mandatory merge-time check**, because no pre-merge test on
   either side can prove the two agree.

The trap was never eliminated. It was converted from a silent quality problem into a single named
check that fails loudly at a known moment.

## What the merge actually found

All three instances finished `DONE`. All three branches merged with **zero conflicts** — ownership
was disjoint, and the only shared file, the coordination file itself, was written section by
section. All 441 tests passed on the merged tree: 155 worker, 194 API, 92 frontend.

Every frozen contract held. Routes, field names, types, status codes, event names, the error
envelope, the SSE frame shape, the Citation object, the cosine operator, tenant scoping in SQL,
column ownership, the claim statement — all verified against real code on both sides.

**And the integrated system answered "I could not find anything about that in your documents" to
every single question.**

### The defect

Contract 4 ends with a test-suite rule: no test may call the live Voyage API, so both instances use
*a deterministic fake embedder* — "seeded hash → 1024 floats → L2-normalize".

Both instances built one. They are not the same function.

- Instance 1: `random.Random(sha256(text)[:8]).gauss()` × 1024
- Instance 2: iterated `sha256` blocks → uint16 → scaled to [−1, 1]

Both are deterministic. Both return exactly 1024 dimensions. Both are exactly L2-normalized. **Both
satisfy every assertion Contract 4 mandates** — which is precisely why neither test suite could
ever have noticed.

They are also mutually orthogonal. Measured cosine between them, on the same input string: **0.045,
−0.022, 0.005**.

The Reconciler proved it through the real production data path — the real worker ingesting a real
document into Postgres, then the real API retrieval querying those chunks, two processes sharing
nothing but the database:

| Question embedded with | Passages returned | Result |
|---|---|---|
| Instance 2's fake (as merged) | **0** | `insufficient_context` — every question refused |
| Instance 1's fake (same algorithm as the stored vectors) | **1**, similarity **1.0** | correct chunk, rank 1 |

Same database, same chunks, same retrieval code, same query bytes. The only variable was which fake
embedded the question.

That second row is the useful half: it proves that **everything else in the two-process pipeline is
correct**. Enqueue, claim, parse, chunk, embed, store, HNSW, cosine ordering, tenant scoping, the
similarity floor, the per-document cap, citation projection — all of it works, end to end, across a
process boundary, on the first try. The fault was isolated to exactly one thing.

### Why this is the interesting result

Production is unaffected: both sides use the real Voyage client, and the fakes are test-only. The
contract was not violated — Contract 4's fake clause said "e.g.", and both readings were legitimate.

What it demonstrates is subtler and more useful than a bug. The contract pinned every value that
would *obviously* cause divergence, and both instances honoured all of them. Divergence arrived
through the one place the contract was deliberately loose — the test double — and it arrived in a
form that satisfied every assertion designed to catch it.

Contract 4's real lesson is not "pin the model id". It is: **when two independently-written programs
must agree about the meaning of a number, every artifact that stands in for that number has to be
pinned too — including the fake.** A test double is part of the interface between two instances the
moment both of them build one.

The mitigation that worked was not a test. It was the decision, made before any code existed, that
a cross-process probe was mandatory at merge and that no green suite could substitute for it.

## What no test could prove

Five boundaries were known in advance to be unprovable before merge, written up as merge-time
checks rather than discovered late:

1. **Vector comparability** across the two embedding clients — the one above.
2. **Real ingestion of a real PDF** — page attribution and heading detection against an actual
   multi-column, header-bearing document, not a synthetic fixture.
3. **Citation truthfulness** — that the passage the UI opens genuinely supports the sentence it is
   attached to. Instance 2 can prove the mapping is consistent; only a human reading a real answer
   can prove it is *true*.
4. **SSE surviving the real deploy path** — streaming is routinely killed by proxy buffering and
   compression middleware. Only a real origin proves it.
5. **The HNSW index actually being used** — pgvector silently falls back to a sequential scan when
   the query's operator does not match the index's operator class. Green tests on 50 fixture rows
   prove nothing.

Instance 2 found (5) partly on its own and reported the nuance honestly: with a few hundred fixture
rows the planner prefers a sort because reading everything genuinely *is* cheaper, so its test
proves the index is *usable*, not that it is *chosen* under production cost. It said so in its work
log rather than claiming the check was done. That distinction — between what a test proves and what
it appears to prove — is the same distinction the fake-embedder defect turns on.

## What worked

- **Freezing the DDL before the worktrees existed.** Two instances mapped the same tables with zero
  drift, and the schema belonged to neither of them.
- **Column ownership as an absolute rule.** Two processes wrote to one database for an entire build
  with no coordination and no collisions.
- **Escalating instead of improvising.** Three ambiguities were found. All three were written up as
  proposed amendments with the implemented reading stated and the one-line revert identified —
  none was silently patched. Two turned out to need no code change at all; the third was a
  documentation gap the consumer had already handled forward-compatibly.
- **Instances auditing their own coverage.** Instance 2's second pass found three contract
  requirements it had implemented but never exercised — the SSE heartbeat, multi-turn history, and
  CORS. All three are merge-breakers, and none had failed.

## What the merge changed

Nothing in any instance's code. The Reconciler wrote the deferred cross-cutting glue — the
assembled `docker-compose.yml`, `DEPLOY.md`, this file, and the root `README.md` — and surfaced
every mismatch to the human rather than picking a winner. Fixing a contract mismatch by editing one
side is how you lose the record of which side was right.
