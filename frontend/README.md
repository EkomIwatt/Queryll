# Queryll — frontend

The browser surface for Queryll: sign-in, the document library with live ingestion status, the
passage viewer, and the chat where a streamed answer assembles itself with clickable citations.

React 19 · Vite · TypeScript (strict) · react-router · Vitest + Testing Library.

This is **Instance 3** of a three-way parallel build. It owns `frontend/` in full and consumes
Contracts 1, 6, 7, 8 and 9 from the frozen `INTERFACE CONTRACTS` block in the repo-root
`CLAUDE.md`. It produces no contract of its own, and it was written without ever seeing the API's
code — the contract text is the specification.

## Running it

```bash
npm install
cp .env.example .env        # point VITE_API_BASE_URL at the API
npm run dev
```

**Without a backend.** The API lives in a separate worktree, so the whole UI can be driven by an
in-memory mock:

```bash
VITE_MOCK_API=1 npm run dev
```

Uploads ingest on a timer (`pending → processing → ready`, with the indeterminate phase), answers
stream token by token with working citations, and asking about something obviously absent — the
weather, say — takes the grounding-refusal path. It is a demo, not a contract simulator: the frozen
contracts are proven at merge against the real API and never against this.

| Script | |
|---|---|
| `npm run dev` | Vite dev server on :5173 |
| `npm run build` | typecheck + production build to `dist/` |
| `npm run typecheck` | `tsc -b --noEmit` |
| `npm test` | the full Vitest suite |

## How it is laid out

```
src/
  api/          types.ts (the contracts, hand-written) · client.ts · sse.ts
                askStream.ts (fetch + ReadableStream) · fakeStream.ts (the scripted reader)
  auth/         in-memory token, refresh-on-load, the sign-in shell
  library/      polling discipline, uploader, ingestion status, document detail
  chat/         the stream state machine, marker parsing, citation chips, sources panel
  passages/     resolving a citation to its passage, in context
  ui/           icons, primitives, tokens-driven theme toggle
  dev/          the in-memory mock backend
  styles/       tokens.css · base.css · app.css
tests/          one suite per seam, all mocking at the API-client boundary
```

## The five decisions worth knowing

**1. The fake stream reader was built before any chat UI.** Everything interesting about the answer
experience is a *timing and framing* property — sources landing before the first token, a mid-stream
error arriving after partial text, a `[n]` marker torn in half by a network chunk boundary. None of
it is reachable through a mocked promise. `src/api/fakeStream.ts` provides three implementations of
the same `AskStreamFn` the production code uses: scripted (events on a timer), manual (the test
emits each one by hand), and raw SSE (real bytes through the real parser, split at byte offsets you
choose). The last one is the only thing that can prove a frame survives being cut in half.

**2. Mocks live at the API-client boundary, not at `fetch`.** `src/api/types.ts` is hand-written
from `CLAUDE.md` — not generated from a server this instance cannot see — and every test fixture is
typed by it. A contract change breaks compilation instead of quietly breaking a string comparison
three layers down.

**3. Citations resolve by `chunk_id` and by nothing else.** Never by index, never by page, never by
matching snippet text. `index` is per-answer, pages are display metadata, and the snippet is a hover
card. A citation whose document has since been deleted or re-indexed 404s, and that is an ordinary
outcome: the viewer says the source is no longer available and still shows what the answer quoted
(Contract 8 §3). The citation is never hidden and never rendered as an error.

**4. `insufficient_context` is an answer, not a failure.** The app working correctly and the app
failing must not look the same, so the grounding-refusal path gets a calm, framed treatment with an
explanation of *why* no answer was produced — and it renders identically when the turn is read back
from the server later, so history stays honest.

**5. An idle library tab issues zero network requests.** Polling runs every 2s only while something
is `pending` or `processing`, stops the instant nothing is, and backs off to 10s after five minutes
of continuous polling (Contract 6 §4). It is enforced by `useDocuments` and tested with fake timers,
because that claim is exactly the kind that quietly stops being true and nobody notices until a rate
limit does.

## Design

"The annotated page." Warm paper rather than the usual cold grey, prose set in **Newsreader** (a
serif with real optical sizing) at a generous measure, UI chrome in **Atkinson Hyperlegible**, and
metadata in **JetBrains Mono**. The single saturated colour is a vermilion that behaves like a
proofreader's pen — it appears only on citations, marginalia and things the reader made happen.

Dark mode is tokens-only: every colour is defined once on `:root` and redefined under both
`prefers-color-scheme` and an explicit `data-theme`, so no component branches on theme. The theme
*preference* is the only thing this app puts in `localStorage`; the access token never goes near it.

Keyboard-reachable throughout — the uploader is a real `<button>` wrapping a real file input, the
citation chips are buttons whose hover cards also open on focus, and the passage viewer is a native
`<dialog>` so Escape, focus trapping and inertness are the platform's job rather than ours.

## Testing

92 tests across 8 suites (`npm test`). Roughly:

- **`sse.test.ts`** — frame decoding: chunk-straddling frames, `: ping` heartbeats, CRLF,
  multi-line `data`, unknown event names ignored, malformed known events thrown.
- **`askStream.test.ts`** — the real `fetch` layer: headers, the single refresh-and-retry on 401,
  the Contract 9 envelope for errors *before* the stream opens, a dropped connection.
- **`markers.test.ts`** — `[n]` parsing, including withholding a half-written marker mid-stream and
  never losing a character.
- **`chat.test.tsx`** — the whole answer experience through the fake reader: sources before tokens,
  token-by-token assembly, chips opening passages by `chunk_id`, uncited sources dimmed,
  `insufficient_context`, mid-stream errors keeping partial text, cancellation.
- **`library.test.tsx`** — the polling discipline with fake timers, the indeterminate-vs-real
  progress distinction, `error_message` rendered verbatim, upload validation, delete confirmation.
- **`passages.test.tsx`** — neighbours, page ranges, and the dangling-citation path.
- **`auth.test.tsx`** — session restore, sign-in, the password minimum, and a half-typed question
  surviving navigation.
- **`uploadRules.test.ts`** — the size and type rules as pure data.

## Known assumptions

- `display_name` falls back to the email local-part (Contract 1, marked ASSUMED there).
- Conversation titles are mirrored locally from the first question so the rail does not read
  "Untitled" until the next load; the server remains the authority (Contract 7 §9, ASSUMED).
- Document scope in the composer defaults to "everything", sending `document_ids: null`.
- Uploads are sent one at a time so library order stays predictable; the API returns 202 immediately
  either way.
