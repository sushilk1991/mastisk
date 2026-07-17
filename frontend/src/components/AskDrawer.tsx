import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { Icon } from './icons';
import { api } from '../api';
import { useModalA11y } from '../hooks/useModalA11y';
import type { AskConversationSummary, AskResponse, AskSource, View } from '../types';

type ChatMode = 'wiki' | 'research';

interface Msg {
  role: 'user' | 'assistant';
  text: string;
  question?: string;
  response?: AskResponse;
  savedNoteId?: number;
}

interface Ctx { prompt: string; selection: string | null; article_id?: string }

interface Props {
  open: boolean;
  ctx: Ctx | null;
  onClose: () => void;
  onNavigate: (view: View, id?: string) => void;
}

export function AskDrawer({ open, ctx, onClose, onNavigate }: Props) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [mode, setMode] = useState<ChatMode>('wiki');
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<AskConversationSummary[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyErr, setHistoryErr] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const endRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const autoPromptRef = useRef<string | null>(null);
  const { modalRef, ariaProps } = useModalA11y({
    open,
    onClose,
    initialFocusRef: inputRef,
  });

  useEffect(() => {
    if (!open) return;
    const prompt = ctx?.prompt.trim();
    if (prompt && autoPromptRef.current !== prompt) {
      autoPromptRef.current = prompt;
      setHistoryOpen(false);
      void send(prompt);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, ctx?.prompt, ctx?.selection]);

  useEffect(() => {
    if (open) void loadHistory();
  }, [open]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [msgs, sending]);

  async function loadHistory() {
    setHistoryErr(null);
    try {
      const result = await api.askHistory.list();
      setConversations(result.conversations);
    } catch (error) {
      setHistoryErr((error as Error).message);
    }
  }

  async function send(
    text?: string,
    historyOverride?: Msg[],
    conversationOverride?: string | null,
  ) {
    const question = (text ?? input).trim();
    if (!question || sending) return;
    const history = historyOverride ?? msgs;
    const userMessage: Msg = { role: 'user', text: question };
    setSending(true);
    setMsgs([...history, userMessage]);
    setInput('');
    try {
      const response = await api.ask(question, {
        selection: ctx?.selection ?? undefined,
        article_id: ctx?.article_id,
        mode,
        conversation_id: conversationOverride === undefined
          ? conversationId ?? undefined
          : conversationOverride ?? undefined,
        messages: history.slice(-10).map((message) => ({
          role: message.role,
          content: message.text,
        })),
      });
      setConversationId(response.conversation_id);
      setMsgs((current) => [
        ...current,
        { role: 'assistant', text: response.answer, response, question },
      ]);
      void loadHistory();
    } catch (error) {
      setMsgs((current) => [
        ...current,
        { role: 'assistant', text: `I couldn't complete that turn: ${(error as Error).message}` },
      ]);
    } finally {
      setSending(false);
    }
  }

  async function openConversation(id: string) {
    setHistoryBusy(true);
    setHistoryErr(null);
    try {
      const conversation = await api.askHistory.get(id);
      let lastQuestion = '';
      const restored = conversation.messages.map((message): Msg => {
        if (message.role === 'user') {
          lastQuestion = message.content;
          return { role: 'user', text: message.content };
        }
        return {
          role: 'assistant',
          text: message.content,
          question: lastQuestion,
          response: message.response ?? undefined,
        };
      });
      autoPromptRef.current = ctx?.prompt.trim() || null;
      setConversationId(conversation.id);
      setMode(conversation.mode);
      setMsgs(restored);
      setHistoryOpen(false);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    } catch (error) {
      setHistoryErr((error as Error).message);
    } finally {
      setHistoryBusy(false);
    }
  }

  async function deleteConversation(id: string) {
    setHistoryBusy(true);
    setHistoryErr(null);
    try {
      await api.askHistory.delete(id);
      if (conversationId === id) resetChat();
      await loadHistory();
    } catch (error) {
      setHistoryErr((error as Error).message);
    } finally {
      setHistoryBusy(false);
    }
  }

  async function saveAsNote(index: number, message: Msg) {
    if (!message.response || message.savedNoteId) return;
    const sourceLines = message.response.sources
      .map((source) => `- ${source.ref}: ${source.title}${source.href ? ` (${source.href})` : ''}`)
      .join('\n');
    const note = await api.notes.create(
      `# From Mastisk Chat\n\n## Question\n${message.question ?? ''}\n\n## Answer\n${message.text}` +
      `\n\n## Sources\n${sourceLines || '(no sources)'}`,
    );
    setMsgs((current) => current.map((row, rowIndex) => (
      rowIndex === index ? { ...row, savedNoteId: note.id } : row
    )));
  }

  function resetChat() {
    autoPromptRef.current = null;
    setMsgs([]);
    setInput('');
    setMode('wiki');
    setConversationId(null);
    setHistoryOpen(false);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }

  const suggestions = mode === 'research'
    ? [
        'Research recent approaches to governing tool-using agents',
        'Find current evidence that challenges this wiki page',
      ]
    : ctx?.selection
    ? [
        `Show everything that mentions "${ctx.selection}"`,
        `What's the strongest counter-view to this selection?`,
      ]
    : [
        'What does my wiki say about agent reliability?',
        'What should I revisit based on my interests?',
      ];

  return (
    <div
      ref={modalRef}
      {...ariaProps}
      className={`ask-drawer ${open ? 'open' : ''}`}
      aria-hidden={!open}
      aria-label="Mastisk chat"
    >
      <div className="ask-head">
        <div className="ask-title">
          {Icon.spark}
          <span>Mastisk Chat</span>
          {ctx?.selection && <span className="ask-context">selection</span>}
        </div>
        <div className="ask-head-actions">
          <button
            type="button"
            aria-pressed={historyOpen}
            disabled={sending}
            onClick={() => setHistoryOpen((value) => !value)}
          >
            History{conversations.length > 0 ? ` ${conversations.length}` : ''}
          </button>
          {msgs.length > 0 && <button type="button" disabled={sending} onClick={resetChat}>New chat</button>}
          <button type="button" className="tb-btn" onClick={onClose} aria-label="Close chat">
            {Icon.close}
          </button>
        </div>
      </div>

      {historyOpen ? (
        <div className="ask-history" aria-label="Past chats">
          <div className="ask-history-intro">
            <div>
              <h2>Past chats</h2>
              <p>Conversations are saved in your local Mastisk database.</p>
            </div>
            <button type="button" className="chip" disabled={sending} onClick={resetChat}>Start a new chat</button>
          </div>
          {historyErr && <p className="dash-error">{historyErr}</p>}
          {conversations.length === 0 && !historyErr ? (
            <div className="ask-history-empty">Your first conversation will appear here.</div>
          ) : (
            <div className="ask-history-list">
              {conversations.map((conversation) => (
                <div
                  key={conversation.id}
                  className={`ask-history-row ${conversation.id === conversationId ? 'active' : ''}`}
                >
                  <button
                    type="button"
                    className="ask-history-open"
                    disabled={historyBusy || sending}
                    onClick={() => void openConversation(conversation.id)}
                  >
                    <strong>{conversation.title}</strong>
                    <span>{historyDate(conversation.updated_at)} · {conversation.mode === 'research' ? 'Wiki + web' : 'Wiki only'}</span>
                    <p>{conversation.preview}</p>
                  </button>
                  <button
                    type="button"
                    className="ask-history-delete"
                    disabled={historyBusy || sending}
                    aria-label={`Delete chat: ${conversation.title}`}
                    title="Delete chat"
                    onClick={() => void deleteConversation(conversation.id)}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
      <>
      <div className="ask-body" aria-live="polite">
        {msgs.length === 0 && !sending && (
          <div className="ask-empty">
            <h2>Ask your second brain.</h2>
            <p>Your wiki is always the starting point. Add live web research when the question needs current evidence.</p>
          </div>
        )}

        {msgs.map((message, index) => (
          <div key={`${message.role}-${index}`} className={`ask-msg ${message.role}`}>
            {message.role === 'user' ? (
              <div className="ask-bubble">{message.text}</div>
            ) : (
              <>
                <div className="ask-text md">
                  <ReactMarkdown components={{
                    a: ({ href, children }) => {
                      const safe = safeHref(href);
                      return safe
                        ? <a href={safe} target={safe.startsWith('http') ? '_blank' : undefined} rel="noreferrer">{children}</a>
                        : <span>{children}</span>;
                    },
                  }}>{message.text}</ReactMarkdown>
                </div>
                {message.response && (
                  <>
                    <div className="ask-answer-meta">
                      {message.response.sources.length > 0
                        ? `${message.response.sources.length} cited · `
                        : 'No inline citations · '}
                      {message.response.research_status === 'unavailable' ? 'web unavailable · ' : ''}
                      {formatCoverage(message.response.coverage)} · {message.response.provider}
                    </div>
                    {message.response.sources.length > 0 && (
                      <div className="ask-cites" aria-label="Cited sources">
                        {message.response.sources.map((source) => (
                          <SourceLink key={`${source.ref}-${source.id}`} source={source} onNavigate={onNavigate} onClose={onClose}/>
                        ))}
                      </div>
                    )}
                    {(message.response.retrieved_sources ?? message.response.sources).length > message.response.sources.length && (
                      <details className="ask-retrieved">
                        <summary>{(message.response.retrieved_sources ?? message.response.sources).length} retrieved sources</summary>
                        <div className="ask-cites">
                          {(message.response.retrieved_sources ?? message.response.sources).map((source) => (
                            <SourceLink key={`retrieved-${source.ref}-${source.id}`} source={source} onNavigate={onNavigate} onClose={onClose}/>
                          ))}
                        </div>
                      </details>
                    )}
                    <button
                      type="button"
                      className="ask-save"
                      onClick={() => void saveAsNote(index, message)}
                      disabled={Boolean(message.savedNoteId)}
                    >
                      {message.savedNoteId ? `Saved as note #${message.savedNoteId}` : 'Save as note'}
                    </button>
                  </>
                )}
              </>
            )}
          </div>
        ))}

        {sending && (
          <div className="ask-msg assistant">
            <div className="ask-progress">
              <span/>
              {mode === 'research' ? 'Searching your wiki and the web…' : 'Reading across your wiki…'}
            </div>
          </div>
        )}

        {msgs.length === 0 && (
          <div className="ask-suggestions">
            {suggestions.map((suggestion) => (
              <button key={suggestion} type="button" onClick={() => void send(suggestion)}>{suggestion}</button>
            ))}
          </div>
        )}
        <div ref={endRef}/>
      </div>

      <div className="ask-compose">
        <div className="ask-mode" aria-label="Sources for this message">
          <button type="button" aria-pressed={mode === 'wiki'} className={mode === 'wiki' ? 'active' : ''} onClick={() => setMode('wiki')}>
            Wiki only
          </button>
          <button type="button" aria-pressed={mode === 'research'} className={mode === 'research' ? 'active' : ''} onClick={() => setMode('research')}>
            Wiki + web
          </button>
          <span>{mode === 'research' ? 'Adds current web evidence to Mastisk context.' : 'Uses Mastisk without searching the web.'}</span>
        </div>
        <div className="ask-input">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder={mode === 'research' ? 'Ask using my wiki and current web evidence…' : 'Ask using my Mastisk wiki…'}
            aria-label="Message Mastisk"
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <button
            type="button"
            className="ask-send"
            onClick={() => void send()}
            disabled={!input.trim() || sending}
            aria-label="Send message"
          >
            {Icon.arrow}
          </button>
        </div>
      </div>
      </>
      )}
    </div>
  );
}

function SourceLink({
  source, onNavigate, onClose,
}: {
  source: AskSource;
  onNavigate: (view: View, id?: string) => void;
  onClose: () => void;
}) {
  const href = safeHref(source.href ?? undefined);
  if (!href) {
    return <span className="ask-cite" title={source.excerpt}>{source.ref} · {source.title}</span>;
  }
  return (
    <a
      className="ask-cite"
      href={href}
      title={source.excerpt}
      target={href.startsWith('http') ? '_blank' : undefined}
      rel="noreferrer"
      onClick={(event) => {
        if (href.startsWith('/a/')) {
          event.preventDefault();
          onNavigate('article', decodeURIComponent(href.slice(3)));
          onClose();
        } else if (href.startsWith('/notes/')) {
          event.preventDefault();
          onNavigate('note', href.slice('/notes/'.length));
          onClose();
        } else if (href.startsWith('/blog/')) {
          event.preventDefault();
          onNavigate('blog_post', href.slice('/blog/'.length));
          onClose();
        }
      }}
    >
      <strong>{source.ref}</strong> · {source.title}
    </a>
  );
}

function safeHref(href?: string): string | undefined {
  if (!href) return undefined;
  return href.startsWith('/') || href.startsWith('https://') || href.startsWith('http://')
    ? href
    : undefined;
}

function formatCoverage(coverage: Record<string, number>): string {
  const labels: Record<string, string> = {
    article: 'article', note: 'note', blog: 'draft', web: 'web source', profile: 'profile file',
  };
  const parts = Object.entries(coverage)
    .filter(([kind]) => kind !== 'overview')
    .map(([kind, count]) => `${count} ${labels[kind] ?? kind}${count === 1 ? '' : 's'}`);
  return parts.length ? parts.join(' · ') : 'No matching sources';
}

function historyDate(value: string): string {
  const date = new Date(value.endsWith('Z') ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
