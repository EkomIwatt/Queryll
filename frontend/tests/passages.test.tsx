/**
 * The passage viewer — Contract 6 §3 and Contract 8 §2/§3.
 *
 * The behaviour that matters most here is the dangling citation: a document that has since been
 * deleted or re-indexed makes its chunk route 404, and that is an ordinary outcome rather than a
 * failure. A historical answer must keep showing what it was based on.
 */

import { describe, expect, it } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiProvider } from '../src/api/ApiProvider';
import { ApiError } from '../src/api/errors';
import { PassageViewer } from '../src/passages/PassageViewer';
import { targetFromCitation } from '../src/passages/usePassage';
import { render } from '@testing-library/react';
import { createMockApi, makeChunk, makeCitation } from './harness';

function renderViewer(api: ReturnType<typeof createMockApi>, citation = makeCitation()) {
  return render(
    <ApiProvider api={api}>
      <PassageViewer target={targetFromCitation(citation)} onClose={() => {}} />
    </ApiProvider>,
  );
}

describe('opening a citation', () => {
  it('fetches by chunk_id and shows the passage with its filename, heading and page', async () => {
    const api = createMockApi({
      chunkDetail: { chunk: makeChunk(), prev: null, next: null },
    });
    renderViewer(api, makeCitation({ chunk_id: 'chunk-a', document_id: 'doc-1' }));

    await waitFor(() => expect(api.getChunk).toHaveBeenCalledWith('doc-1', 'chunk-a'));
    expect(await screen.findByText(/reduces variance/)).toBeInTheDocument();
    expect(screen.getByText('sampling-methods.pdf')).toBeInTheDocument();
    expect(screen.getAllByText('3. Methods > 3.2 Sampling').length).toBeGreaterThan(0);
    expect(screen.getByText('p. 12')).toBeInTheDocument();
  });

  it('shows a page range when a chunk spans a page break', async () => {
    const api = createMockApi({
      chunkDetail: {
        chunk: makeChunk({ page_start: 12, page_end: 13 }),
        prev: null,
        next: null,
      },
    });
    renderViewer(api, makeCitation({ page_start: 12, page_end: 13 }));

    expect(await screen.findByText('pp. 12–13')).toBeInTheDocument();
  });

  it('shows no page chip for a format that has no pages', async () => {
    const api = createMockApi({
      chunkDetail: {
        chunk: makeChunk({ page_start: null, page_end: null }),
        prev: null,
        next: null,
      },
    });
    renderViewer(api, makeCitation({ page_start: null, page_end: null, filename: 'notes.md' }));

    await screen.findByText(/reduces variance/);
    expect(screen.queryByText(/^pp?\. /)).not.toBeInTheDocument();
  });

  it('offers the neighbouring passages and walks to them by id', async () => {
    const api = createMockApi({
      chunkDetail: {
        chunk: makeChunk(),
        prev: makeChunk({ id: 'chunk-prev', ordinal: 16, text: 'The preceding paragraph.' }),
        next: makeChunk({ id: 'chunk-next', ordinal: 18, text: 'The following paragraph.' }),
      },
    });
    renderViewer(api);
    const user = userEvent.setup();

    expect(await screen.findByText('The preceding paragraph.')).toBeInTheDocument();
    expect(screen.getByText('The following paragraph.')).toBeInTheDocument();

    await user.click(screen.getByText('The following paragraph.'));
    await waitFor(() => expect(api.getChunk).toHaveBeenLastCalledWith('doc-1', 'chunk-next'));
  });

  it('marks the edges of the document', async () => {
    const api = createMockApi({ chunkDetail: { chunk: makeChunk(), prev: null, next: null } });
    renderViewer(api);

    expect(await screen.findByText('Start of the document')).toBeInTheDocument();
    expect(screen.getByText('End of the document')).toBeInTheDocument();
  });
});

describe('a dangling citation (Contract 8 §3)', () => {
  it('explains that the source is gone and still shows what the answer quoted', async () => {
    const api = createMockApi();
    api.getChunk.mockRejectedValue(new ApiError('That was not found.', 404));
    renderViewer(api, makeCitation({ snippet: 'The quoted sentence from the deleted document.' }));

    expect(
      await screen.findByText(/source is no longer available/i),
    ).toBeInTheDocument();
    // The citation is neither hidden nor turned into an error.
    expect(
      screen.getByText('The quoted sentence from the deleted document.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    // The filename it cited is still named.
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('sampling-methods.pdf')).toBeInTheDocument();
  });

  it('does show an error for a genuine failure, not a 404', async () => {
    const api = createMockApi();
    api.getChunk.mockRejectedValue(
      new ApiError('Something went wrong on the server. Please try again.', 500),
    );
    renderViewer(api);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Something went wrong on the server. Please try again.',
    );
    expect(screen.queryByText(/no longer available/i)).not.toBeInTheDocument();
  });
});
