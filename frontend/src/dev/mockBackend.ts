/**
 * A dev-only in-memory Queryll API.
 *
 * The real backend lives in another worktree and does not exist here, so this exists to make the
 * frontend genuinely demonstrable on its own: `VITE_MOCK_API=1 npm run dev` gives a working
 * library, a real ingestion progression, and a streamed, cited answer — driven by the same
 * `AskStreamFn` the tests use.
 *
 * It is a *demo*, not a contract simulator and not a substitute for integration. It implements the
 * shapes in `types.ts` and nothing more; the frozen contracts are proven at merge against Instance
 * 2's real API, never against this.
 */

import type { QueryllApi } from '../api/client';
import { ApiError } from '../api/errors';
import type {
  AskEvent,
  AuthSession,
  Chunk,
  Citation,
  Conversation,
  Document,
  Message,
  User,
} from '../api/types';
import { INSUFFICIENT_CONTEXT_SENTENCE } from '../api/types';

const USER: User = {
  id: 'demo-user',
  email: 'demo@queryll.local',
  display_name: 'demo',
  created_at: new Date('2026-09-01T09:00:00Z').toISOString(),
};

const SESSION: AuthSession = { access_token: 'demo-token', user: USER };

const SAMPLE_TEXT = [
  'Stratified sampling divides the population into mutually exclusive strata before drawing from each one, which reduces variance whenever the strata are internally homogeneous.',
  'Cluster sampling, by contrast, draws whole groups and accepts the loss of precision in exchange for a far cheaper field operation.',
  'The trade-off is therefore between statistical efficiency and the cost of reaching the units at all, and the right answer depends on how the population is physically distributed.',
  'Weighting is applied after collection so that each stratum contributes in proportion to its share of the population rather than its share of the sample.',
];

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function createMockBackend(): QueryllApi {
  const documents: Document[] = [];
  const chunksByDocument = new Map<string, Chunk[]>();
  const conversations: Conversation[] = [];
  const messagesByConversation = new Map<string, Message[]>();
  let counter = 0;
  const id = (prefix: string) => `${prefix}-${(counter += 1)}`;

  /** Walk a document from `pending` through `processing` to `ready`, the way the worker would. */
  function ingest(document: Document): void {
    const chunks: Chunk[] = SAMPLE_TEXT.map((text, ordinal) => ({
      id: id('chunk'),
      ordinal,
      page_start: document.mime_type === 'application/pdf' ? ordinal + 1 : null,
      page_end: document.mime_type === 'application/pdf' ? ordinal + 1 : null,
      heading_path: ordinal < 2 ? '3. Methods > 3.2 Sampling' : null,
      preview: text.slice(0, 200),
      text,
      token_count: Math.round(text.length / 4),
    }));

    let step = 0;
    const advance = () => {
      step += 1;
      if (step === 1) {
        document.status = 'processing';
        document.progress = 0; // parsing — no chunk count known yet (Contract 3 §7)
      } else if (step <= 1 + chunks.length) {
        document.progress = Math.min(0.99, (step - 1) / chunks.length);
      } else {
        document.status = 'ready';
        document.progress = 1;
        document.page_count = document.mime_type === 'application/pdf' ? chunks.length : null;
        document.chunk_count = chunks.length;
        document.indexed_at = new Date().toISOString();
        chunksByDocument.set(document.id, chunks);
        return;
      }
      setTimeout(advance, 1_200);
    };
    setTimeout(advance, 900);
  }

  function ownChunks(documentId: string): Chunk[] {
    const chunks = chunksByDocument.get(documentId);
    if (!chunks) throw new ApiError('That was not found.', 404);
    return chunks;
  }

  return {
    async signup() {
      return SESSION;
    },
    async login() {
      return SESSION;
    },
    async refresh() {
      return SESSION;
    },
    async logout() {
      /* nothing to forget */
    },
    async me() {
      return USER;
    },

    async uploadDocument(file) {
      await delay(250);
      const document: Document = {
        id: id('doc'),
        filename: file.name,
        mime_type: file.type || 'text/plain',
        size_bytes: file.size,
        status: 'pending',
        progress: 0,
        page_count: null,
        chunk_count: null,
        error_message: null,
        created_at: new Date().toISOString(),
        indexed_at: null,
      };
      documents.unshift(document);
      ingest(document);
      return { ...document };
    },

    async listDocuments() {
      return { documents: documents.map((d) => ({ ...d })), next_cursor: null };
    },

    async getDocument(documentId) {
      const found = documents.find((d) => d.id === documentId);
      if (!found) throw new ApiError('That was not found.', 404);
      return { ...found };
    },

    async listChunks(documentId) {
      const chunks = ownChunks(documentId);
      return {
        chunks: chunks.map(({ text: _text, token_count: _tokens, ...summary }) => summary),
        next_cursor: null,
      };
    },

    async getChunk(documentId, chunkId) {
      const chunks = ownChunks(documentId);
      const index = chunks.findIndex((c) => c.id === chunkId);
      if (index === -1) throw new ApiError('That was not found.', 404);
      return {
        chunk: chunks[index]!,
        prev: chunks[index - 1] ?? null,
        next: chunks[index + 1] ?? null,
      };
    },

    async reindexDocument(documentId) {
      const found = documents.find((d) => d.id === documentId);
      if (!found) throw new ApiError('That was not found.', 404);
      if (found.status === 'processing') {
        throw new ApiError('That document is being processed right now.', 409);
      }
      chunksByDocument.delete(documentId);
      Object.assign(found, {
        status: 'pending',
        progress: 0,
        error_message: null,
        indexed_at: null,
        chunk_count: null,
      });
      ingest(found);
      return { ...found };
    },

    async deleteDocument(documentId) {
      const index = documents.findIndex((d) => d.id === documentId);
      if (index === -1) throw new ApiError('That was not found.', 404);
      if (documents[index]!.status === 'processing') {
        throw new ApiError('That document is being processed right now.', 409);
      }
      documents.splice(index, 1);
      chunksByDocument.delete(documentId);
    },

    async createConversation(title = null) {
      const conversation: Conversation = {
        id: id('conv'),
        title,
        created_at: new Date().toISOString(),
      };
      conversations.unshift(conversation);
      messagesByConversation.set(conversation.id, []);
      return { ...conversation };
    },

    async listConversations() {
      return { conversations: conversations.map((c) => ({ ...c })) };
    },

    async getConversation(conversationId) {
      const conversation = conversations.find((c) => c.id === conversationId);
      if (!conversation) throw new ApiError('That was not found.', 404);
      return {
        conversation: { ...conversation },
        messages: messagesByConversation.get(conversationId) ?? [],
      };
    },

    async deleteConversation(conversationId) {
      const index = conversations.findIndex((c) => c.id === conversationId);
      if (index === -1) throw new ApiError('That was not found.', 404);
      conversations.splice(index, 1);
      messagesByConversation.delete(conversationId);
    },

    async *ask(input) {
      const scope = input.body.document_ids;
      const pool = documents.filter(
        (d) => d.status === 'ready' && (scope === null || scope.includes(d.id)),
      );

      const sources: Citation[] = pool
        .flatMap((document) =>
          (chunksByDocument.get(document.id) ?? []).slice(0, 2).map((chunk) => ({ document, chunk })),
        )
        .slice(0, 4)
        .map(({ document, chunk }, i): Citation => ({
          index: i + 1,
          chunk_id: chunk.id,
          document_id: document.id,
          filename: document.filename,
          page_start: chunk.page_start,
          page_end: chunk.page_end,
          heading_path: chunk.heading_path,
          similarity: Number((0.86 - i * 0.09).toFixed(3)),
          snippet: chunk.text.slice(0, 300),
        }));

      // Anything mentioning something obviously absent takes the grounding-refusal path, so the
      // Contract 7 §5 experience is reachable by hand.
      const grounded = sources.length > 0 && !/capital of|weather|stock price/i.test(input.body.question);

      if (!grounded) {
        yield { event: 'retrieval', data: { sources: [], insufficient_context: true } };
        await delay(300);
        yield { event: 'token', data: { text: INSUFFICIENT_CONTEXT_SENTENCE } };
        yield { event: 'done', data: { message_id: id('msg'), citations_used: [] } };
        return;
      }

      yield { event: 'retrieval', data: { sources, insufficient_context: false } };
      await delay(700); // the model thinking, before the first token

      const answer =
        `The documents describe two designs. Stratified sampling splits the population into strata ` +
        `and draws from each, which lowers variance when the strata are homogeneous [1]. Cluster ` +
        `sampling instead draws whole groups, trading precision for a much cheaper field operation ` +
        `[${Math.min(2, sources.length)}].\n\n` +
        `Which is right depends on how the population is physically distributed, and on what ` +
        `reaching the units actually costs [1].`;

      for (const word of answer.split(/(?<=\s)/)) {
        await delay(22);
        yield { event: 'token', data: { text: word } } satisfies AskEvent;
      }

      const messageId = id('msg');
      const thread = messagesByConversation.get(input.conversationId) ?? [];
      thread.push(
        {
          id: id('msg'),
          role: 'user',
          content: input.body.question,
          citations: [],
          created_at: new Date().toISOString(),
        },
        {
          id: messageId,
          role: 'assistant',
          content: answer,
          citations: sources,
          created_at: new Date().toISOString(),
        },
      );
      messagesByConversation.set(input.conversationId, thread);

      const conversation = conversations.find((c) => c.id === input.conversationId);
      if (conversation && !conversation.title) {
        conversation.title = input.body.question.slice(0, 60);
      }

      yield {
        event: 'done',
        data: { message_id: messageId, citations_used: [1, Math.min(2, sources.length)] },
      };
    },
  };
}
