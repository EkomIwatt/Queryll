/**
 * The library, and the polling discipline — Contract 6 §4 and Contract 3 §4/§7.
 *
 * The polling rules are tested with fake timers rather than by inspection, because "an idle tab
 * issues zero requests" is precisely the kind of claim that is true when written and quietly false
 * three refactors later, and nobody notices until a rate limit does.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AppRoutes } from '../src/routes';
import { ApiError } from '../src/api/errors';
import type { Document } from '../src/api/types';
import { createMockApi, makeDocument, renderWithProviders } from './harness';

function renderLibrary(api: ReturnType<typeof createMockApi>) {
  renderWithProviders(<AppRoutes />, { api, route: '/library' });
}

/** Flush pending promises and timers together — fake timers stall promise chains otherwise. */
async function tick(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

afterEach(() => {
  vi.useRealTimers();
});

describe('polling discipline', () => {
  it('issues exactly one request for a library with nothing in flight, and none after', async () => {
    vi.useFakeTimers();
    const api = createMockApi({ documents: [makeDocument({ status: 'ready' })] });
    renderLibrary(api);

    await tick();
    expect(api.listDocuments).toHaveBeenCalledTimes(1);

    // A fully-`ready` library must not arm a timer at all. Five minutes of silence.
    await tick(300_000);
    expect(api.listDocuments).toHaveBeenCalledTimes(1);
  });

  it('polls every 2 seconds while a document is still being ingested', async () => {
    vi.useFakeTimers();
    const api = createMockApi({
      documents: [makeDocument({ status: 'processing', progress: 0.3, chunk_count: null })],
    });
    renderLibrary(api);

    await tick();
    expect(api.listDocuments).toHaveBeenCalledTimes(1);

    await tick(2_000);
    expect(api.listDocuments).toHaveBeenCalledTimes(2);

    await tick(2_000);
    expect(api.listDocuments).toHaveBeenCalledTimes(3);

    // And not faster than that.
    await tick(1_000);
    expect(api.listDocuments).toHaveBeenCalledTimes(3);
  });

  it('stops polling the moment the last document becomes ready', async () => {
    vi.useFakeTimers();
    const processing = makeDocument({ status: 'processing', progress: 0.5, chunk_count: null });
    const api = createMockApi({ documents: [processing] });
    api.listDocuments
      .mockResolvedValueOnce({ documents: [processing], next_cursor: null })
      .mockResolvedValueOnce({ documents: [processing], next_cursor: null })
      .mockResolvedValue({ documents: [makeDocument({ status: 'ready' })], next_cursor: null });

    renderLibrary(api);
    await tick();
    await tick(2_000); // poll 2 — still processing
    await tick(2_000); // poll 3 — now ready
    expect(api.listDocuments).toHaveBeenCalledTimes(3);

    await tick(60_000);
    expect(api.listDocuments).toHaveBeenCalledTimes(3);
  });

  it('backs the interval off to 10 seconds after five minutes of continuous polling', async () => {
    vi.useFakeTimers();
    const api = createMockApi({
      documents: [makeDocument({ status: 'processing', progress: 0.1, chunk_count: null })],
    });
    renderLibrary(api);

    await tick();
    // Advanced two seconds at a time, because each poll only arms the next one after its response
    // has been rendered — which is exactly how it behaves against a real API.
    for (let i = 0; i < 150; i += 1) await tick(2_000);

    const atFiveMinutes = api.listDocuments.mock.calls.length;
    // 1 initial + a poll every 2s for five minutes.
    expect(atFiveMinutes).toBe(151);

    // Two seconds is no longer enough to trigger the next one.
    await tick(2_000);
    expect(api.listDocuments).toHaveBeenCalledTimes(atFiveMinutes);

    await tick(8_000);
    expect(api.listDocuments).toHaveBeenCalledTimes(atFiveMinutes + 1);
  });

  it('keeps polling after a failed poll rather than giving up silently', async () => {
    vi.useFakeTimers();
    const processing = makeDocument({ status: 'processing', progress: 0.2, chunk_count: null });
    const api = createMockApi({ documents: [processing] });
    api.listDocuments
      .mockResolvedValueOnce({ documents: [processing], next_cursor: null })
      .mockRejectedValueOnce(new ApiError('Something went wrong on the server.', 500))
      .mockResolvedValue({ documents: [processing], next_cursor: null });

    renderLibrary(api);
    await tick();
    await tick(2_000); // this one rejects
    await tick(2_000);

    expect(api.listDocuments).toHaveBeenCalledTimes(3);
  });
});

describe('ingestion, made visible (Contract 3 §7)', () => {
  it('shows an indeterminate bar for `processing` at progress 0.0', async () => {
    const api = createMockApi({
      documents: [makeDocument({ status: 'processing', progress: 0, chunk_count: null })],
    });
    renderLibrary(api);

    const bar = await screen.findByRole('progressbar');
    // No fraction is knowable yet — parsing happens before any chunk count exists.
    expect(bar).not.toHaveAttribute('aria-valuenow');
    expect(bar).toHaveClass('progress--indeterminate');
    expect(screen.getByText('Reading the document')).toBeInTheDocument();
  });

  it('shows a real bar once a fraction of the chunks is embedded', async () => {
    const api = createMockApi({
      documents: [makeDocument({ status: 'processing', progress: 0.42, chunk_count: null })],
    });
    renderLibrary(api);

    const bar = await screen.findByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '42');
    expect(screen.getByText('42%')).toBeInTheDocument();
  });

  it('renders a failure message verbatim and offers a retry that re-indexes', async () => {
    const message = 'This PDF is password-protected and could not be read.';
    const api = createMockApi({
      documents: [
        makeDocument({
          status: 'failed',
          progress: 0,
          chunk_count: null,
          page_count: null,
          indexed_at: null,
          error_message: message,
        }),
      ],
    });
    renderLibrary(api);
    const user = userEvent.setup();

    // Verbatim: the worker wrote this sentence for the reader, and the UI must not reword it.
    expect(await screen.findByText(message)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(api.reindexDocument).toHaveBeenCalledWith('doc-1'));
  });

  it('reports how many passages a ready document was split into', async () => {
    const api = createMockApi({ documents: [makeDocument({ chunk_count: 61, page_count: 24 })] });
    renderLibrary(api);

    expect(await screen.findByText(/61/)).toBeInTheDocument();
    expect(screen.getByText(/passages indexed/)).toBeInTheDocument();
  });

  it('refuses to offer delete or re-index while the worker holds the document', async () => {
    // Contract 6 §5/§6 return 409 for both; disabling avoids a pointless round-trip to an error.
    const api = createMockApi({
      documents: [makeDocument({ status: 'processing', progress: 0.6, chunk_count: null })],
    });
    renderLibrary(api);

    expect(
      await screen.findByRole('button', { name: /^Delete sampling-methods\.pdf$/ }),
    ).toBeDisabled();
    expect(screen.getByRole('button', { name: /^Re-index sampling-methods\.pdf$/ })).toBeDisabled();
  });
});

describe('the empty library', () => {
  it('teaches what the app is instead of shrugging', async () => {
    const api = createMockApi({ documents: [] });
    renderLibrary(api);

    expect(await screen.findByText('Give Queryll something to read.')).toBeInTheDocument();
    expect(screen.getByText('Upload a document')).toBeInTheDocument();
    expect(screen.getByText('Ask, and check the answer')).toBeInTheDocument();
    // Nothing to ask about yet, so no invitation to.
    expect(screen.queryByRole('link', { name: /Ask a question/ })).not.toBeInTheDocument();
  });
});

describe('uploading', () => {
  it('accepts a supported file and places it in the library as pending', async () => {
    const api = createMockApi({ documents: [] });
    renderLibrary(api);
    const user = userEvent.setup();

    const input = await screen.findByLabelText('Choose documents to upload');
    const file = new File(['%PDF-1.7 ...'], 'notes.pdf', { type: 'application/pdf' });
    await user.upload(input, file);

    await waitFor(() => expect(api.uploadDocument).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('notes.pdf')).toBeInTheDocument();
    expect(await screen.findByText('Queued')).toBeInTheDocument();
  });

  it('refuses an unsupported type locally, without a round-trip', async () => {
    const api = createMockApi({ documents: [] });
    renderLibrary(api);

    const input = await screen.findByLabelText('Choose documents to upload');
    // `applyAccept: false` so the file actually reaches the component: the browser's `accept`
    // filter is a hint, and a drag-and-drop or a renamed file bypasses it entirely. Our own
    // validation is what has to catch this.
    await userEvent
      .setup({ applyAccept: false })
      .upload(input, new File(['MZ'], 'installer.exe', { type: 'application/x-msdownload' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'installer.exe is not a supported file type. Upload a PDF, .txt or .md file.',
    );
    expect(api.uploadDocument).not.toHaveBeenCalled();
  });

  it('renders the server sentence when the server rejects an upload the client let through', async () => {
    const api = createMockApi({ documents: [] });
    api.uploadDocument.mockRejectedValueOnce(
      new ApiError('That file type is not supported. Upload a PDF, .txt or .md file.', 415),
    );
    renderLibrary(api);
    const user = userEvent.setup();

    const input = await screen.findByLabelText('Choose documents to upload');
    await user.upload(input, new File(['not really a pdf'], 'lying.pdf', { type: 'application/pdf' }));

    // The server sniffs content and is always the authority, whatever the client concluded.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'That file type is not supported. Upload a PDF, .txt or .md file.',
    );
  });
});

describe('deleting', () => {
  it('confirms first, then deletes and removes the document from the list', async () => {
    const documents: Document[] = [makeDocument()];
    const api = createMockApi({ documents });
    renderLibrary(api);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole('button', { name: /^Delete sampling-methods\.pdf$/ }),
    );

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/citations will no longer open/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: 'Delete permanently' }));
    await waitFor(() => expect(api.deleteDocument).toHaveBeenCalledWith('doc-1'));
    await waitFor(() => expect(screen.queryByText('sampling-methods.pdf')).not.toBeInTheDocument());
  });

  it('surfaces the 409 sentence when a delete races the worker', async () => {
    const api = createMockApi({ documents: [makeDocument()] });
    api.deleteDocument.mockRejectedValueOnce(
      new ApiError('That document is being processed right now. Try again once it finishes.', 409),
    );
    renderLibrary(api);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole('button', { name: /^Delete sampling-methods\.pdf$/ }),
    );
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Delete permanently' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'That document is being processed right now. Try again once it finishes.',
    );
  });
});
