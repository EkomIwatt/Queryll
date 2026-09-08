/**
 * The chat — Contract 7.
 *
 * Three things this screen is built around, in the order they matter:
 *
 *  1. Sources appear before the answer. The `retrieval` frame lands first by contract, so the
 *     apparatus is painted while the model is still writing.
 *  2. `insufficient_context` is an *answer*, not an error. Queryll saying "that isn't in your
 *     documents" is the app working exactly as designed, and it must not look like a failure.
 *  3. A mid-stream error keeps the partial text and puts the sentence underneath it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useApi } from '../api/ApiProvider';
import { messageFor } from '../api/errors';
import type { Citation, Conversation, Message } from '../api/types';
import { useDocuments } from '../library/useDocuments';
import { PassageViewer } from '../passages/PassageViewer';
import { targetFromCitation, type PassageTarget } from '../passages/usePassage';
import { AnswerText } from './AnswerText';
import { Composer } from './Composer';
import { SourcesPanel } from './SourcesPanel';
import { citedIndices } from './markers';
import { NEW_CONVERSATION, draftStore } from './draftStore';
import { useAsk } from './useAsk';
import { EmptyState, ErrorNotice, InfoNotice, Spinner } from '../ui/primitives';
import { AskIcon, PlusIcon, TrashIcon } from '../ui/icons';
import { formatDate } from '../ui/format';

export function ChatPage() {
  const api = useApi();
  const navigate = useNavigate();
  const { conversationId } = useParams<{ conversationId?: string }>();
  const library = useDocuments();
  const { state, ask, cancel, reset, completedMessages } = useAsk();

  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [loadingThread, setLoadingThread] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const [passage, setPassage] = useState<PassageTarget | null>(null);

  const draftKey = conversationId ?? NEW_CONVERSATION;
  const [draft, setDraft] = useState(() => draftStore.get(draftKey));
  /** A conversation we just created — its thread is known-empty, so skip the fetch. */
  const freshlyCreated = useRef<string | null>(null);
  const threadEnd = useRef<HTMLDivElement>(null);

  const readyDocuments = useMemo(
    () => library.documents.filter((d) => d.status === 'ready'),
    [library.documents],
  );
  const someIngesting = library.documents.some(
    (d) => d.status === 'pending' || d.status === 'processing',
  );

  /* ---- conversations rail ---- */

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const list = await api.listConversations();
        if (!cancelled) setConversations(list.conversations);
      } catch (cause) {
        if (!cancelled) setPageError(messageFor(cause));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api]);

  /* ---- the open thread ---- */

  useEffect(() => {
    setDraft(draftStore.get(conversationId ?? NEW_CONVERSATION));

    if (!conversationId) {
      setMessages([]);
      return;
    }
    if (freshlyCreated.current === conversationId) {
      // We created this conversation a moment ago, so its thread is known-empty and there is
      // nothing to fetch. Crucially it must not be *cleared* either: a fast stream can finish and
      // commit its turn before this effect runs, and blanking here would erase the answer the
      // reader is looking at.
      freshlyCreated.current = null;
      return;
    }

    let cancelled = false;
    setLoadingThread(true);
    void (async () => {
      try {
        const detail = await api.getConversation(conversationId);
        if (cancelled) return;
        setMessages(detail.messages);
        setPageError(null);
      } catch (cause) {
        if (!cancelled) setPageError(messageFor(cause));
      } finally {
        if (!cancelled) setLoadingThread(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, conversationId]);

  /* ---- commit a finished turn into the thread ---- */

  useEffect(() => {
    if (state.phase !== 'done') return;
    const pair = completedMessages();
    if (!pair) return;
    setMessages((current) => [...current, ...pair]);

    // The server titles a null-titled conversation from the first question (Contract 7 §9);
    // mirroring it locally keeps the rail from showing "Untitled" until the next page load.
    const [userMessage] = pair;
    if (conversationId) {
      setConversations((current) =>
        current.map((c) =>
          c.id === conversationId && !c.title
            ? { ...c, title: userMessage.content.slice(0, 60) }
            : c,
        ),
      );
    }
    reset();
  }, [state.phase, completedMessages, reset, conversationId]);

  useEffect(() => {
    threadEnd.current?.scrollIntoView({ block: 'end' });
  }, [messages.length, state.text, state.phase]);

  /* ---- asking ---- */

  const handleSubmit = useCallback(
    async (question: string, documentIds: string[] | null) => {
      setPageError(null);
      let id = conversationId;

      try {
        if (!id) {
          const created = await api.createConversation(null);
          id = created.id;
          freshlyCreated.current = id;
          setConversations((current) => [created, ...current]);
          draftStore.rekey(NEW_CONVERSATION, id);
          navigate(`/ask/${id}`, { replace: true });
        }
      } catch (cause) {
        setPageError(messageFor(cause));
        return;
      }

      // The question is committed the moment it is sent, so it is never lost to a failure below.
      draftStore.clear(NEW_CONVERSATION);
      draftStore.clear(id);
      setDraft('');

      await ask({ conversationId: id, question, documentIds });
    },
    [api, conversationId, ask, navigate],
  );

  const handleDraftChange = useCallback(
    (value: string) => {
      setDraft(value);
      draftStore.set(conversationId ?? NEW_CONVERSATION, value);
    },
    [conversationId],
  );

  const openCitation = useCallback((citation: Citation) => {
    setPassage(targetFromCitation(citation));
  }, []);

  const deleteConversation = useCallback(
    async (id: string) => {
      try {
        await api.deleteConversation(id);
        setConversations((current) => current.filter((c) => c.id !== id));
        if (id === conversationId) navigate('/ask', { replace: true });
      } catch (cause) {
        setPageError(messageFor(cause));
      }
    },
    [api, conversationId, navigate],
  );

  const busy = state.phase === 'retrieving' || state.phase === 'streaming';
  const liveTurn = state.phase !== 'idle';

  return (
    <div className="chat">
      <aside className="chat__rail" aria-label="Conversations">
        <Link className="btn btn--secondary chat__new" to="/ask">
          <PlusIcon size={15} />
          New question
        </Link>

        <ol className="convos">
          {conversations.map((conversation) => (
            <li key={conversation.id} className="convos__item">
              <Link
                to={`/ask/${conversation.id}`}
                className={`convos__link${
                  conversation.id === conversationId ? ' convos__link--active' : ''
                }`}
              >
                <span className="convos__title">{conversation.title ?? 'Untitled question'}</span>
                <span className="convos__date">{formatDate(conversation.created_at)}</span>
              </Link>
              <button
                type="button"
                className="btn btn--ghost btn--small convos__delete"
                aria-label={`Delete conversation ${conversation.title ?? 'Untitled question'}`}
                onClick={() => void deleteConversation(conversation.id)}
              >
                <TrashIcon size={14} />
              </button>
            </li>
          ))}
        </ol>
      </aside>

      <main className="chat__main" id="main">
        {pageError ? <ErrorNotice>{pageError}</ErrorNotice> : null}

        <div className="thread">
          {loadingThread ? (
            <div className="thread__loading">
              <Spinner label="Loading this conversation" size={20} />
            </div>
          ) : null}

          {!loadingThread && messages.length === 0 && !liveTurn ? (
            readyDocuments.length === 0 ? (
              <EmptyState
                eyebrow="Nothing to search yet"
                title={
                  someIngesting
                    ? 'Your documents are still being read.'
                    : 'Queryll has nothing to answer from.'
                }
                action={
                  <Link className="btn btn--primary" to="/library">
                    Go to your library
                  </Link>
                }
              >
                <p>
                  {someIngesting
                    ? 'Indexing usually takes a few seconds. Your library shows the progress, and this page will let you ask as soon as the first document is ready.'
                    : 'Upload a PDF, a text file or a Markdown file first. Queryll only answers from documents you have given it — it will never fall back on general knowledge.'}
                </p>
              </EmptyState>
            ) : (
              <EmptyState
                eyebrow="Ask"
                title="What do you want to know?"
                action={
                  <ul className="prompts">
                    <li>“What does this say about &lt;topic&gt;, and where?”</li>
                    <li>“Summarise the section on &lt;topic&gt; with citations.”</li>
                    <li>“Does anything here contradict &lt;claim&gt;?”</li>
                  </ul>
                }
              >
                <p>
                  Answers are drawn only from the {readyDocuments.length} document
                  {readyDocuments.length === 1 ? '' : 's'} in your library, and every claim will
                  carry a citation you can open.
                </p>
              </EmptyState>
            )
          ) : null}

          {messages.map((message) => (
            <MessageBlock key={message.id} message={message} onOpenCitation={openCitation} />
          ))}

          {liveTurn ? (
            <LiveTurn
              question={state.question}
              sources={state.sources}
              insufficientContext={state.insufficientContext}
              text={state.text}
              citationsUsed={state.phase === 'done' ? state.citationsUsed : null}
              phase={state.phase}
              error={state.error}
              onOpenCitation={openCitation}
            />
          ) : null}

          <div ref={threadEnd} />
        </div>

        <div className="chat__composer">
          {readyDocuments.length === 0 && library.status === 'ready' ? (
            <InfoNotice>
              {someIngesting
                ? 'Your documents are still being indexed. You can ask as soon as one is ready.'
                : 'Add a document to your library before asking a question.'}
            </InfoNotice>
          ) : null}

          <Composer
            value={draft}
            onChange={handleDraftChange}
            onSubmit={(question, ids) => void handleSubmit(question, ids)}
            readyDocuments={readyDocuments}
            busy={busy}
            onCancel={cancel}
            disabled={readyDocuments.length === 0}
          />
        </div>
      </main>

      <PassageViewer target={passage} onClose={() => setPassage(null)} />
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * A persisted turn
 * ------------------------------------------------------------------ */

function MessageBlock({
  message,
  onOpenCitation,
}: {
  message: Message;
  onOpenCitation: (citation: Citation) => void;
}) {
  if (message.role === 'user') {
    return (
      <section className="turn turn--question">
        <p className="eyebrow">You asked</p>
        <h2 className="turn__question">{message.content}</h2>
      </section>
    );
  }

  // `citations_used` is not persisted (Contract 7 §9 stores the full array), so which sources this
  // answer leaned on is re-derived from its own markers — the same information, read back.
  const used = citedIndices(message.content, message.citations.length);

  // An assistant message with no citations at all is the grounding-refusal path (Contract 7 §5):
  // no chunk cleared the floor, so Claude was never called. It has to keep reading as a calm
  // answer when it is read back later, exactly as it did while it arrived.
  const insufficient = message.citations.length === 0;

  return (
    <section className="turn turn--answer">
      <SourcesPanel sources={message.citations} citationsUsed={used} onOpen={onOpenCitation} />
      <AnswerBody
        text={message.content}
        sources={message.citations}
        insufficientContext={insufficient}
        onOpenCitation={onOpenCitation}
      />
    </section>
  );
}

/**
 * The answer itself — shared by the turn currently streaming and every turn read back from the
 * server, so the two can never drift into looking like different things.
 */
function AnswerBody({
  text,
  sources,
  insufficientContext,
  streaming = false,
  onOpenCitation,
}: {
  text: string;
  sources: readonly Citation[];
  insufficientContext: boolean;
  streaming?: boolean;
  onOpenCitation: (citation: Citation) => void;
}) {
  if (insufficientContext) {
    return (
      <div className="grounding">
        <p className="eyebrow">No grounded answer</p>
        <p className="grounding__body">{text || '…'}</p>
        <p className="grounding__note">
          Nothing in your documents was close enough to the question to answer from, so Queryll did
          not ask the model to guess. Try rephrasing, or add the document that covers this.
        </p>
      </div>
    );
  }

  return (
    <AnswerText
      text={text}
      sources={sources}
      streaming={streaming}
      onOpenCitation={onOpenCitation}
    />
  );
}

/* ------------------------------------------------------------------ *
 * The turn currently arriving
 * ------------------------------------------------------------------ */

function LiveTurn({
  question,
  sources,
  insufficientContext,
  text,
  citationsUsed,
  phase,
  error,
  onOpenCitation,
}: {
  question: string | null;
  sources: Citation[];
  insufficientContext: boolean;
  text: string;
  citationsUsed: number[] | null;
  phase: string;
  error: string | null;
  onOpenCitation: (citation: Citation) => void;
}) {
  const streaming = phase === 'streaming' || phase === 'retrieving';

  return (
    <>
      {question ? (
        <section className="turn turn--question">
          <p className="eyebrow">You asked</p>
          <h2 className="turn__question">{question}</h2>
        </section>
      ) : null}

      <section className="turn turn--answer" aria-live="polite" aria-busy={streaming}>
        {phase === 'retrieving' ? (
          <p className="turn__retrieving">
            <AskIcon size={15} />
            Searching your documents…
          </p>
        ) : null}

        <SourcesPanel sources={sources} citationsUsed={citationsUsed} onOpen={onOpenCitation} />

        {/* Contract 7 §5 — a calm, honest answer, not an error. */}
        <AnswerBody
          text={text}
          sources={sources}
          insufficientContext={insufficientContext}
          streaming={streaming}
          onOpenCitation={onOpenCitation}
        />

        {/* The partial answer above stays exactly where it is. */}
        {error ? <ErrorNotice>{error}</ErrorNotice> : null}
      </section>
    </>
  );
}
