/**
 * The answer experience — Contract 7, end to end through the real component tree.
 *
 * Everything here runs through the fake stream reader, so the *timing* of the contract is under
 * test and not merely its final output: that `retrieval` paints before the first token, that a
 * mid-stream error keeps the text that already arrived, that `insufficient_context` reads as an
 * answer rather than a failure.
 */

import { describe, expect, it } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AppRoutes } from '../src/routes';
import {
  createManualAskStream,
  createRawSseAskStream,
  createScriptedAskStream,
  sseBodyText,
} from '../src/api/fakeStream';
import { NetworkError } from '../src/api/errors';
import { INSUFFICIENT_CONTEXT_SENTENCE } from '../src/api/types';
import type { QueryllApi } from '../src/api/client';
import {
  askScript,
  createMockApi,
  makeCitation,
  makeChunk,
  makeDocument,
  renderWithProviders,
} from './harness';

const READY_DOCS = [makeDocument()];

async function askQuestion(question = 'How is the sample stratified?') {
  const user = userEvent.setup();
  const box = await screen.findByLabelText('Your question');
  // The composer renders disabled until the library has loaded and something is `ready` — waiting
  // for that is part of the behaviour, not a test detail.
  await waitFor(() => expect(box).not.toBeDisabled());
  await user.type(box, question);
  await user.click(screen.getByRole('button', { name: 'Ask' }));
  return user;
}

function renderChat(ask: QueryllApi['ask'], overrides: Parameters<typeof createMockApi>[0] = {}) {
  const api = createMockApi({ documents: READY_DOCS, ask, ...overrides });
  renderWithProviders(<AppRoutes />, { api, route: '/ask' });
  return api;
}

describe('the answer stream', () => {
  it('paints the retrieved sources before a single token has arrived', async () => {
    const manual = createManualAskStream();
    renderChat(manual.stream);
    await askQuestion();

    await manual.opened();
    await manual.emit({
      event: 'retrieval',
      data: { sources: [makeCitation(), makeCitation({ index: 2, chunk_id: 'chunk-b' })], insufficient_context: false },
    });

    // The apparatus is on screen while the model is still writing — this is the contract's
    // "retrieval is ALWAYS FIRST", and it is most of the perceived speed of the app.
    const panel = await screen.findByRole('complementary', {
      name: 'Passages this answer was drawn from',
    });
    expect(within(panel).getAllByRole('listitem')).toHaveLength(2);
    expect(screen.queryByText(/stratified by region/i)).not.toBeInTheDocument();

    await manual.emit({ event: 'token', data: { text: 'Stratified by region.' } });
    expect(await screen.findByText(/stratified by region\./i)).toBeInTheDocument();

    manual.close();
  });

  it('assembles the answer token by token, in order', async () => {
    const manual = createManualAskStream();
    renderChat(manual.stream);
    await askQuestion();
    await manual.opened();

    await manual.emit({ event: 'retrieval', data: { sources: [makeCitation()], insufficient_context: false } });
    await manual.emit({ event: 'token', data: { text: 'The sample ' } });
    await manual.emit({ event: 'token', data: { text: 'is stratified ' } });
    await manual.emit({ event: 'token', data: { text: 'by region.' } });

    await waitFor(() =>
      expect(screen.getByText(/by region\./)).toBeInTheDocument(),
    );
    const answer = document.querySelector('.answer')!;
    expect(answer.textContent).toBe('The sample is stratified by region.');

    manual.close();
  });

  it('sends the question and the document scope the contract specifies', async () => {
    const seen: unknown[] = [];
    const stream = createScriptedAskStream(askScript(), {
      onRequest: (input) => seen.push(input.body),
    });
    renderChat(stream);
    await askQuestion('Where is stratification described?');

    await waitFor(() => expect(seen).toHaveLength(1));
    // `document_ids: null` means "search all of the caller's documents" (Contract 7 §1), and the
    // client never sends a user id — ownership comes from the token.
    expect(seen[0]).toEqual({
      question: 'Where is stratification described?',
      document_ids: null,
    });
  });

  it('renders a [n] marker as a citation chip that opens the passage by chunk_id', async () => {
    const citation = makeCitation({ chunk_id: 'chunk-xyz', document_id: 'doc-7' });
    const api = renderChat(
      createScriptedAskStream(
        askScript({ sources: [citation], tokens: ['Stratified by region ', '[1]', '.'] }),
      ),
      { chunkDetail: { chunk: makeChunk({ id: 'chunk-xyz' }), prev: null, next: null } },
    );
    const user = await askQuestion();

    const chip = await screen.findByRole('button', { name: /^Source 1,.*Open the passage\.$/ });
    await user.click(chip);

    // Contract 8 §2: resolved by chunk_id, never by index, page or snippet text.
    await waitFor(() => expect(api.getChunk).toHaveBeenCalledWith('doc-7', 'chunk-xyz'));
    expect(await screen.findByText(/reduces variance/i)).toBeInTheDocument();
  });

  it('dims the sources the finished answer did not cite, without hiding them', async () => {
    const sources = [
      makeCitation({ index: 1, chunk_id: 'c1' }),
      makeCitation({ index: 2, chunk_id: 'c2', filename: 'appendix.md' }),
    ];
    renderChat(
      createScriptedAskStream(
        askScript({ sources, tokens: ['Only the first matters ', '[1]', '.'], citationsUsed: [1] }),
      ),
    );
    await askQuestion();

    const unusedNotes = await screen.findAllByText('not cited');
    expect(unusedNotes).toHaveLength(1);
    // Dimmed, still present, still openable.
    expect(screen.getByText('appendix.md')).toBeInTheDocument();
  });

  it('reassembles a marker split across two network chunks into one chip', async () => {
    const citation = makeCitation();
    const body = sseBodyText([
      { event: 'retrieval', data: { sources: [citation], insufficient_context: false } },
      { event: 'token', data: { text: 'Stratified by region [1].' } },
      { event: 'done', data: { message_id: 'm1', citations_used: [1] } },
    ]);
    const cut = body.indexOf('[1]') + 1;
    renderChat(createRawSseAskStream([body.slice(0, cut), body.slice(cut)]));
    await askQuestion();

    const chips = await screen.findAllByRole('button', { name: /Open the passage\.$/ });
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveTextContent('1');
    // And no half-marker or stray bracket left in the prose. (The hover card lives inside the
    // paragraph too, so the prose is read from the text runs rather than from `textContent`.)
    const prose = [...document.querySelectorAll('.answer__para > span:not(.cite)')]
      .map((node) => node.textContent)
      .join('');
    expect(prose).toBe('Stratified by region .');
    expect(document.querySelector('.answer')!.textContent).not.toMatch(/\[1\]/);
  });
});

describe('the grounding rule (Contract 7 §5)', () => {
  it('renders insufficient_context as a calm answer, not as an error', async () => {
    renderChat(
      createScriptedAskStream([
        { event: 'retrieval', data: { sources: [], insufficient_context: true } },
        { event: 'token', data: { text: INSUFFICIENT_CONTEXT_SENTENCE } },
        { event: 'done', data: { message_id: 'm1', citations_used: [] } },
      ]),
    );
    await askQuestion('What is the capital of France?');

    expect(await screen.findByText(INSUFFICIENT_CONTEXT_SENTENCE)).toBeInTheDocument();
    expect(screen.getByText(/did not ask the model to guess/i)).toBeInTheDocument();
    // The app working correctly and the app failing must not look the same.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    // No sources apparatus either: Claude was never called, so there is nothing to show.
    expect(
      screen.queryByRole('complementary', { name: 'Passages this answer was drawn from' }),
    ).not.toBeInTheDocument();
  });
});

describe('failures mid-stream', () => {
  it('keeps the partial answer and shows the error beneath it', async () => {
    const manual = createManualAskStream();
    renderChat(manual.stream);
    await askQuestion();
    await manual.opened();

    await manual.emit({ event: 'retrieval', data: { sources: [makeCitation()], insufficient_context: false } });
    await manual.emit({ event: 'token', data: { text: 'The sample is stratified' } });
    await manual.emit({
      event: 'error',
      data: { error: 'The answer service stopped responding.', code: 'upstream' },
    });
    manual.close();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The answer service stopped responding.');
    // The text the reader already saw is still there.
    expect(screen.getByText(/The sample is stratified/)).toBeInTheDocument();
  });

  it('treats a dropped connection as an error while keeping what arrived', async () => {
    const manual = createManualAskStream();
    renderChat(manual.stream);
    await askQuestion();
    await manual.opened();

    await manual.emit({ event: 'retrieval', data: { sources: [makeCitation()], insufficient_context: false } });
    await manual.emit({ event: 'token', data: { text: 'Partially written' } });
    manual.fail(new NetworkError());

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not reach the server/i);
    expect(screen.getByText(/Partially written/)).toBeInTheDocument();
  });

  it('does not leave the reader waiting when the body closes with no terminal frame', async () => {
    const manual = createManualAskStream();
    renderChat(manual.stream);
    await askQuestion();
    await manual.opened();

    await manual.emit({ event: 'retrieval', data: { sources: [makeCitation()], insufficient_context: false } });
    await manual.emit({ event: 'token', data: { text: 'Half an answer' } });
    manual.close();

    expect(await screen.findByRole('alert')).toHaveTextContent(/stopped before it was finished/i);
  });
});

describe('asking with nothing to search', () => {
  it('explains, and disables the composer, when no document is ready', async () => {
    const api = createMockApi({
      documents: [makeDocument({ status: 'failed', error_message: 'x', chunk_count: null })],
    });
    renderWithProviders(<AppRoutes />, { api, route: '/ask' });

    expect(await screen.findByText(/Queryll has nothing to answer from/i)).toBeInTheDocument();
    expect(await screen.findByLabelText('Your question')).toBeDisabled();
    expect(api.ask).not.toHaveBeenCalled();
  });

  it('says indexing is still running when documents are in flight', async () => {
    const api = createMockApi({
      documents: [makeDocument({ status: 'processing', progress: 0.4, chunk_count: null })],
    });
    renderWithProviders(<AppRoutes />, { api, route: '/ask' });

    expect(
      await screen.findByText(/Your documents are still being read/i),
    ).toBeInTheDocument();
  });
});

describe('conversation handling', () => {
  it('creates a conversation on the first question and streams into it', async () => {
    const api = renderChat(createScriptedAskStream(askScript()));
    await askQuestion();

    await waitFor(() => expect(api.createConversation).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(1));
    const calls = (api.ask as unknown as { mock: { calls: [{ conversationId: string }][] } }).mock
      .calls;
    expect(calls[0]?.[0]?.conversationId).toBe('conv-new');
  });

  it('commits the finished turn into the thread', async () => {
    renderChat(createScriptedAskStream(askScript({ tokens: ['A settled answer.'] })));
    await askQuestion('A question worth keeping');

    expect(await screen.findByText('A settled answer.')).toBeInTheDocument();
    expect(screen.getByText('A question worth keeping')).toBeInTheDocument();
    // The live cursor is gone once the turn is committed.
    await waitFor(() => expect(document.querySelector('.answer__cursor')).toBeNull());
  });

  it('renders a stored answer with its citations re-derived from its own markers', async () => {
    const api = createMockApi({
      documents: READY_DOCS,
      conversations: [],
      messages: [
        {
          id: 'm-user',
          role: 'user',
          content: 'What does it say about stratification?',
          citations: [],
          created_at: '2026-09-06T10:00:00Z',
        },
        {
          id: 'm-assistant',
          role: 'assistant',
          content: 'It is stratified by region [1]. Nothing uses [2].',
          citations: [
            makeCitation({ index: 1, chunk_id: 'c1' }),
            makeCitation({ index: 2, chunk_id: 'c2', filename: 'appendix.md' }),
          ],
          created_at: '2026-09-06T10:00:05Z',
        },
      ],
    });
    renderWithProviders(<AppRoutes />, { api, route: '/ask/conv-1' });

    // Both markers resolve, so both sources count as used and neither is dimmed.
    const chips = await screen.findAllByRole('button', { name: /Open the passage\.$/ });
    expect(chips).toHaveLength(2);
    expect(screen.queryByText('not cited')).not.toBeInTheDocument();
  });
});

describe('cancelling', () => {
  it('stops reading without discarding what already arrived', async () => {
    const manual = createManualAskStream();
    renderChat(manual.stream);
    const user = await askQuestion();
    await manual.opened();

    await manual.emit({ event: 'retrieval', data: { sources: [makeCitation()], insufficient_context: false } });
    await manual.emit({ event: 'token', data: { text: 'Started writing' } });

    await user.click(await screen.findByRole('button', { name: 'Stop this answer' }));

    // Contract 7 §8: the server finishes and persists regardless, so nothing is lost by letting go.
    expect(screen.getByText(/Started writing/)).toBeInTheDocument();
    await waitFor(() => expect(manual.lastInput?.signal?.aborted).toBe(true));
  });
});
