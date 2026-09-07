# Queryll — ingestion worker

A standalone Python process that turns uploaded document bytes into searchable, citable
vectors. No web framework, no HTTP surface, no broker. It talks to exactly two things:
Postgres, and the Voyage embeddings API.

It never calls the API process and the API process never calls it. **The only medium between
them is rows in Postgres** — the schema in `db/init.sql` (Contract 2) and the document
lifecycle protocol (Contract 3). There is no shared filesystem, no shared package, and no
import crossing the `worker/` ↔ `backend/` line.

```
documents.content (bytea)
        │
        ▼
   parse ──► extract text, strip running headers/footers, find columns and headings
        │
        ▼
   chunk ──► structure first, size second; ~512 tokens, ~64 overlap; spans, never strings
        │
        ▼
   embed ──► Voyage voyage-4, input_type="document", 1024d, checked on arrival
        │
        ▼
   chunks + documents.status='ready'      (one transaction)
```

## Running it

```bash
docker compose up -d db                 # from the repo root; host port 5433, not 5432
cp .env.example .env                    # then set VOYAGE_API_KEY
pip install -r requirements.txt
python -m queryll_worker
```

Without a Voyage key, set `EMBEDDING_PROVIDER=fake` to run the whole pipeline end to end. The
fake embedder produces deterministic noise, so the documents will reach `ready` and retrieval
over them will be meaningless — it exercises plumbing, not quality.

## The two things that decide whether this product is any good

### Chunking

Contract 5. Splitting happens on **structure first and size second**: headings, then
paragraphs, then sentences, and only then packing to a token target within those units. A
chunk that ends mid-clause retrieves badly and reads as broken when a user opens it under a
citation.

Three decisions are worth calling out.

**Chunks are spans, never rebuilt strings.** Every unit the packer moves is a
`(char_start, char_end)` pair into one canonical text, and a chunk is
`(first_unit.start, last_unit.end)`. That makes Contract 5 §5's promise —
`text[c.char_start:c.char_end] == c.text` — true *by construction* rather than by careful
arithmetic, which matters because the overlap logic is exactly where that invariant is
normally lost to an off-by-one. It is property-tested over every fixture.

**Running headers and footers are stripped before chunking.** A journal's running title and
page number appear on every page at the same height. Left in, they land in every chunk, drag
every embedding toward the same boilerplate, and read as noise inside a citation. They are
detected by repetition at a stable vertical position, with digit runs normalized so "Page 3 of
12" and "Page 4 of 12" compare equal. Documents shorter than three pages are left alone:
"repeats on every page" is not evidence when there are two pages.

**Heading detection fails closed.** PDF headings come from font size and weight relative to
the document's body text. When the signal is weak — no size variation, or so many lines
qualifying that "heading" has stopped meaning anything — detection is switched off for the
whole document and every chunk gets `heading_path = NULL`. A wrong heading is printed under a
citation as if it were fact, so no heading is strictly better than a plausible wrong one.

The same instinct demotes a document's **title**. A paper's title is the largest type on the
page, so font-based detection quite correctly calls it a heading — and then every citation in
the document reads `Whole Paper Title > 3. Methods > 3.2 Sampling`, with a first segment that
repeats what the filename already says. When the evidence is unambiguous (the first line is a
heading, it is the only one at the largest size, and there are levels beneath it) the title
becomes content, and paths come out as Contract 5's own example has them.

Reading order is solved before any of that: a vertical whitespace gutter splits a page into
columns, and page-spanning lines split it into bands first, so a full-width title above a
two-column body does not hide the gutter underneath it.

### Embeddings

Contract 4, and it is the contract that fails *silently*. This worker embeds chunks; the API
process embeds questions. Two programs, written independently, that never import each other.
If they disagree about provider, model, dimension, input type or normalization, nothing
crashes — retrieval quietly returns near-random passages, Claude answers faithfully from them,
and both test suites stay green because each side is internally consistent.

What this side does about it:

- The frozen values are literals in `config.py`, read from the same env var names with the
  same defaults as the API side.
- **Every vector is checked at the boundary where it enters the program** — 1024 dimensions,
  unit L2 norm — and a failure *raises*. Written as an explicit `raise`, not an `assert`,
  because `assert` is compiled out under `python -O` and this is the last line of defence.
- A Contract 4 violation is **fatal to the process**, not to the document. The worker requeues
  the job untouched, logs `CONTRACT 4 VIOLATION`, and exits non-zero. Grinding on would fail
  every document in the queue one at a time with no explanation; a crash-loop is loud, and
  loud is the point.
- `input_type="document"` is never dropped. If the API ever rejects the parameter the client
  surfaces a message saying to escalate, rather than retrying without it — dropping it on one
  side only is precisely the asymmetry the contract exists to prevent.

**No test here calls the live Voyage API.** The real client is exercised exactly once, at
merge, by the ★ cross-process cosine probe.

#### The deterministic fake embedder

Both Python instances use one, so the two fixture worlds are at least generated the same way.
The algorithm is part of the cross-instance contract, so it is spelled out rather than left to
be reverse-engineered from `fake.py`:

```python
seed   = int.from_bytes(sha256(text.encode("utf-8")).digest()[:8], "big")
rng    = random.Random(seed)
raw    = [rng.gauss(0.0, 1.0) for _ in range(1024)]
vector = [x / l2_norm(raw) for x in raw]
```

Stable across processes, platforms and Python versions. Not semantically meaningful — two
paraphrases get unrelated vectors — which keeps anyone from mistaking a green suite for
evidence about retrieval quality.

## The job loop

Contract 3, and it must survive being killed.

- Jobs are claimed with the exact `FOR UPDATE SKIP LOCKED` statement in Contract 3 §3, kept
  verbatim in `queue.py` because it is contract text.
- **Ingestion is idempotent.** The chunk delete and the chunk insert share one transaction, so
  a job that runs twice leaves exactly the database a job that ran once would: same ordinals,
  same offsets, same text. Only the chunk *ids* differ, which is why Contract 8 resolves
  citations at answer time and never caches an id across a re-index.
- **There is no lock heartbeat.** A run that is killed leaves its job `running`, and the
  fifteen-minute reclaim window hands it back. Contract 5 §1's limits are chosen so a
  worst-case document finishes well inside that window, and refreshing `locked_at` would trade
  the contract's simple, testable recovery story for a hung-but-alive worker that never gets
  reclaimed at all.
- **Permanent failures do not burn three attempts.** Encrypted PDFs, scanned image-only PDFs,
  zero-byte files and files whose extension lies about their content fail immediately with
  copy the user can act on. OCR is out of scope, and a scanned PDF says so.
- `documents.error_message` is **user-facing copy**, rendered verbatim by the UI. Never a
  traceback, never a provider payload, never a path or a key fragment (Contract 9). Internal
  detail goes to `ingestion_jobs.last_error` and the logs instead.
- Progress is written after every embedding batch, and the batch size shrinks for small
  documents so the bar moves roughly eight times per document. A bar that sits at 0 for forty
  seconds and then jumps to done is, to the user, indistinguishable from a hang.

One worker runs in production (a Render **Background Worker** — no port, no health check). The
claim query is correct under N workers and the tests prove it, so scaling later is a config
change rather than a rewrite.

## Layout

```
queryll_worker/
  config.py            frozen Contract 4/5 constants and env loading
  models.py            SQLAlchemy models for the three tables this process touches
  db.py                async engine, sessions, pgvector codec registration
  queue.py             the Contract 3 claim statement and every write the worker makes
  pipeline.py          one job: parse -> chunk -> embed -> commit
  runner.py            the poll loop, signals, and the Contract 4 halt
  errors.py            permanent vs transient, and user-facing copy
  quality.py           the retrieval-quality harness
  parsing/
    base.py            ExtractedDocument, blocks, page spans
    text.py            plain text and Markdown
    pdf.py             PDF text assembly, paragraphs, page attribution
    pdf_layout.py      columns, running headers/footers, heading detection
  chunking/
    tokenizer.py       deterministic token estimation
    sentences.py       sentence, word and line spans
    chunker.py         units, packing, overlap, merge-forward
tools/
  make_fixtures.py     regenerates the PDF and Markdown fixtures
  quality_harness.py   runs the quality cases, lexically or against real Voyage
```

## Tests

```bash
docker compose up -d db          # from the repo root
pip install -r requirements-dev.txt
pytest
pytest -m "not postgres"         # parsing, chunking and embedding only — no database needed
```

Anything touching SQL runs against **real Postgres with pgvector**. There is no in-memory
shortcut in this project: SQLite cannot represent a `vector` column.

The suite creates and uses its **own database** on that server (`queryll_worker_test`), applied
from the frozen `db/init.sql`, rather than the shared `queryll` development database. The
container is shared infrastructure and the API instance develops against it too; a test suite
that truncated its tables would be reaching outside its lane.

What the suite is actually for:

| Area | What it proves |
|---|---|
| `test_chunking.py` | The offset invariant over every fixture, and determinism — twice in one process and twice in fresh interpreters with `PYTHONHASHSEED=random`. |
| `test_pdf_parsing.py` | Against a genuinely messy PDF: columns read in order, running headers gone, page numbers right, hyphenation repaired, headings right or absent. |
| `test_embeddings.py` | The Contract 4 guard raises; the client sends the frozen parameters, reassembles responses by index, and backs off on 429/5xx. |
| `test_queue.py` | Two workers really started against one queued job, with the first transaction held open. Reclaim, cascade, idempotent replace, cosine round-trip. |
| `test_pipeline.py` | `pending → processing → ready` with progress visible mid-run; permanent failures that do not burn three attempts; a third attempt that fails for good. |
| `test_quality.py` | Whether the passage that answers a question survives chunking in one piece — the only test that would catch a chunking change making the product worse. |

One test in `test_chunking.py` is a **complexity guard** rather than a correctness check: it
chunks a 1.3 MB document and fails if that takes more than twenty seconds. An innocuous
`text[:offset]` inside the sentence splitter once copied the whole document at every sentence
boundary, which took a two-megabyte file from one second to over seven minutes — invisible on
any fixture, and fatal at the 20 MB Contract 5 §1 allows. The bound is loose on purpose: it is
there to catch a change of complexity class, not to measure the machine.

Regenerating fixtures (rarely needed — they are committed):

```bash
python tools/make_fixtures.py
```

## Things a merger should double-check

- **The cross-process cosine probe.** Nothing in this repository proves this worker and the
  API agree about what a vector means. That check is merge-time by construction.
- **`input_type` against the live API.** Contract 4 asks for this to be verified on first
  implementation; there is no Voyage key in this worktree, so it is verified by the merge-time
  probe instead. The client fails loudly rather than silently dropping the parameter if the
  API rejects it.
- **Token counts are estimates**, not Voyage BPE counts, so `chunks.token_count` is
  approximate by design (see `chunking/tokenizer.py` for why exactness would break Contract 5
  §4). Nothing depends on it being exact, but it is worth knowing before someone reads it as
  gospel.
- **The kill-the-worker check has been run here, against the real process** — killed mid-run,
  left `running` with no chunks written, reclaimed after the window on attempt 2, finished with
  exactly one set of 1200 chunks. Worth repeating on Render, where the restart is real.
