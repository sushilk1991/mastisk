import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Icon } from './icons';
import { api } from '../api';
import type { SearchResult, View } from '../types';

interface Props {
  open: boolean;
  onClose: () => void;
  /** Called when the user escalates the query to the AI panel. */
  onAsk: (query: string) => void;
  /** Same shape as App's `navigate` so the palette can jump to results. */
  onNavigate: (view: View, id?: string) => void;
}

const KIND_GLYPH: Record<SearchResult['kind'], string> = {
  article: '▲',
  note: '✎',
  blog: '✍',
};

const DEBOUNCE_MS = 180;
const MIN_QUERY_LEN = 2;
const RESULT_LIMIT = 20;

export function CommandPalette({ open, onClose, onAsk, onNavigate }: Props) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Reset everything on open. We don't reset on close so a re-open can in
  // theory restore the prior query, but right now we always clear — keeps
  // each invocation a fresh start, which matches palette-tool conventions.
  useEffect(() => {
    if (!open) return;
    setQuery('');
    setResults([]);
    setError(null);
    setActive(0);
    setLoading(false);
    // Wait one frame for the input to mount, then grab focus.
    const t = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [open]);

  // Debounced search. Aborts in-flight requests when the query changes faster
  // than the network can keep up — otherwise out-of-order responses can
  // overwrite a newer query's results with a stale page.
  useEffect(() => {
    if (!open) return;
    const trimmed = query.trim();
    if (trimmed.length < MIN_QUERY_LEN) {
      setResults([]);
      setLoading(false);
      setError(null);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    const timer = window.setTimeout(() => {
      api.search(trimmed, { limit: RESULT_LIMIT, signal: ctrl.signal })
        .then((r) => {
          setResults(r.results);
          setActive(0);
          setError(null);
        })
        .catch((e: unknown) => {
          // AbortError is expected — user typed another character.
          if (e instanceof Error && e.name === 'AbortError') return;
          const msg = e instanceof Error ? e.message : 'search failed';
          setError(msg);
          setResults([]);
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [query, open]);

  // Keep the highlighted row visible when arrow-keys push it out of view.
  useEffect(() => {
    if (!listRef.current) return;
    const items = listRef.current.querySelectorAll<HTMLElement>('[data-result]');
    items[active]?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  if (!open) return null;

  function handleSelect(r: SearchResult) {
    if (r.kind === 'article') onNavigate('article', r.id);
    else if (r.kind === 'note') onNavigate('note', r.id);
    else if (r.kind === 'blog') onNavigate('blog_post', r.id);
    onClose();
  }

  function handleAsk() {
    const q = query.trim();
    if (!q) return;
    onClose();
    onAsk(q);
  }

  function handleKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => (results.length === 0 ? 0 : Math.min(a + 1, results.length - 1)));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      // ⌘+Enter / Ctrl+Enter is the explicit "ask AI" shortcut even when a
      // result is highlighted — so the user can override the navigate default.
      if (e.metaKey || e.ctrlKey) {
        handleAsk();
        return;
      }
      const hit = results[active];
      if (hit) handleSelect(hit);
      else handleAsk();
    }
  }

  const trimmed = query.trim();
  const hasQuery = trimmed.length >= MIN_QUERY_LEN;
  const showAskFooter = trimmed.length >= 1;

  return (
    <div
      className="cmd-overlay"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-label="Search and command palette"
    >
      <div className="cmd-palette">
        <div className="cmd-input-row">
          <span className="cmd-icon" aria-hidden>{Icon.search}</span>
          <input
            ref={inputRef}
            className="cmd-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKey}
            placeholder="Search articles, notes, blog posts…"
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            aria-label="Search query"
          />
          <kbd>Esc</kbd>
        </div>

        <div className="cmd-body" ref={listRef}>
          {!hasQuery && !loading && (
            <div className="cmd-hint">
              Type to search your wiki, notes, and blog posts.
              {' '}Press <kbd>↵</kbd> on a typed query to ask AI instead.
            </div>
          )}

          {loading && results.length === 0 && (
            <div className="cmd-hint">Searching…</div>
          )}

          {error && (
            <div className="cmd-hint cmd-error">Search failed: {error}</div>
          )}

          {hasQuery && !loading && !error && results.length === 0 && (
            <div className="cmd-hint">
              No matches for <strong>{trimmed}</strong>.
              {' '}Press <kbd>↵</kbd> to ask AI.
            </div>
          )}

          {results.map((r, i) => (
            <div
              key={`${r.kind}-${r.id}`}
              data-result
              className={`cmd-result ${i === active ? 'active' : ''}`}
              onMouseEnter={() => setActive(i)}
              onClick={() => handleSelect(r)}
              role="option"
              aria-selected={i === active}
            >
              <span className="cmd-glyph" aria-hidden>{KIND_GLYPH[r.kind] ?? '◇'}</span>
              <div className="cmd-result-text">
                <div className="cmd-title">{r.title}</div>
                {r.snippet && (
                  <div className="cmd-snippet">{renderHighlighted(r.snippet)}</div>
                )}
              </div>
              <span className="cmd-subtitle">{r.subtitle}</span>
            </div>
          ))}
        </div>

        {showAskFooter && (
          <div
            className="cmd-footer"
            onClick={handleAsk}
            role="button"
            tabIndex={-1}
            aria-label={`Ask AI about ${trimmed}`}
          >
            <span className="cmd-glyph" aria-hidden>{Icon.spark}</span>
            <span className="cmd-footer-text">
              Ask AI about <strong>"{trimmed}"</strong>
            </span>
            <kbd>⌘↵</kbd>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Render a snippet that contains literal `<mark>...</mark>` tags from FTS5
 * `snippet()`. We can't use dangerouslySetInnerHTML because the inner text
 * comes from user content — we'd need to HTML-escape everything else around
 * the marks. Splitting into React nodes lets React handle escaping for us.
 */
function renderHighlighted(snippet: string): ReactNode {
  const parts: ReactNode[] = [];
  const OPEN = '<mark>';
  const CLOSE = '</mark>';
  let i = 0;
  let key = 0;
  while (i < snippet.length) {
    const start = snippet.indexOf(OPEN, i);
    if (start === -1) {
      parts.push(snippet.slice(i));
      break;
    }
    if (start > i) parts.push(snippet.slice(i, start));
    const end = snippet.indexOf(CLOSE, start + OPEN.length);
    if (end === -1) {
      // Malformed — show the rest as plain text so we never lose content.
      parts.push(snippet.slice(start));
      break;
    }
    parts.push(<mark key={key++}>{snippet.slice(start + OPEN.length, end)}</mark>);
    i = end + CLOSE.length;
  }
  return parts;
}
