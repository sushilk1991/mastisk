import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { BlogPostSummary, View } from '../types';
import { TopicSuggestionsPanel } from './TopicSuggestionsPanel';

interface Props {
  onCreateBlog: () => void;
  onCreateBlogWithTheme: (theme: string) => void;
  onNavigate: (view: View, id?: string) => void;
}

const PAGE_SIZE = 50;

export function BlogListView({ onCreateBlog, onCreateBlogWithTheme, onNavigate }: Props) {
  const [rows, setRows] = useState<BlogPostSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [atEnd, setAtEnd] = useState(false);

  useEffect(() => {
    api.blog.list({ limit: PAGE_SIZE })
      .then((data) => {
        setRows(data);
        if (data.length < PAGE_SIZE) setAtEnd(true);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : 'failed'));
  }, []);

  const loadMore = useCallback(async () => {
    if (!rows || rows.length === 0 || loadingMore || atEnd) return;
    setLoadingMore(true);
    try {
      const before = rows[rows.length - 1]?.id;
      const next = await api.blog.list({ limit: PAGE_SIZE, before });
      setRows([...rows, ...next]);
      if (next.length < PAGE_SIZE) setAtEnd(true);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed');
    } finally {
      setLoadingMore(false);
    }
  }, [rows, loadingMore, atEnd]);

  if (err) return <div className="view"><p style={{ color: 'var(--danger, crimson)' }}>{err}</p></div>;
  if (!rows) return <div className="view"><p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading…</p></div>;

  if (rows.length === 0) {
    return (
      <div className="view">
        <TopicSuggestionsPanel onUseSuggestion={onCreateBlogWithTheme} />
        <div className="view-h">Blog Posts</div>
        <h1 className="view-title">No drafts yet</h1>
        <p className="view-sub">
          Blog Writer turns a 7-to-90 day slice of your notes, articles, and roundtable syntheses into a
          first-person draft, with every non-trivial claim citing a source. Pick an optional theme to anchor
          the post, or let the writer find the strongest recurring thread across your recent work.
        </p>
        <button
          onClick={onCreateBlog}
          style={{
            padding: '10px 18px',
            fontSize: 14,
            fontWeight: 600,
            background: 'var(--accent)',
            color: 'var(--fg-inv, white)',
            border: '1px solid var(--accent)',
            borderRadius: 6,
            cursor: 'pointer',
          }}
        >
          Generate your first blog post
        </button>
        <p className="view-sub" style={{ marginTop: 12, fontSize: 11 }}>
          Or run <code>mastisk blog "optional theme"</code> from the CLI.
        </p>
      </div>
    );
  }

  return (
    <div className="view">
      <TopicSuggestionsPanel onUseSuggestion={onCreateBlogWithTheme} />
      <div className="view-h">Blog Posts</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <h1 className="view-title" style={{ margin: 0 }}>
          {rows.length} draft{rows.length === 1 ? '' : 's'}
        </h1>
        <button onClick={onCreateBlog}>+ new draft</button>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
        {rows.map((r) => <Row key={r.id} row={r} onNavigate={onNavigate} />)}
      </div>

      {!atEnd && rows.length > 0 && (
        <div style={{ display: 'flex', justifyContent: 'center', marginTop: 16 }}>
          <button onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? 'loading…' : 'load more'}
          </button>
        </div>
      )}
    </div>
  );
}

function Row({ row, onNavigate }: { row: BlogPostSummary; onNavigate: Props['onNavigate'] }) {
  const statusColor =
    row.status === 'failed' ? 'var(--danger, crimson)'
    : row.status === 'done' ? 'var(--fg)'
    : 'var(--fg-faint)';

  const displayTitle = row.title?.trim()
    ? row.title
    : row.status === 'failed'
      ? 'Draft failed'
      : row.status === 'done'
        ? 'Untitled draft'
        : 'Draft in progress';

  return (
    <button
      onClick={() => onNavigate('blog_post', String(row.id))}
      style={{
        textAlign: 'left', padding: 10,
        border: '1px solid var(--line)', borderRadius: 6,
        background: 'var(--bg-elev, transparent)',
        cursor: 'pointer',
      }}
    >
      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)' }}>
        #{row.id} · {new Date(row.created_at).toLocaleString()} · {row.window_days}d window
        {' · '}
        <span style={{ color: statusColor }}>{row.status}</span>
        {row.model && <> · {row.model}</>}
        {row.word_count !== null && row.word_count !== undefined && <> · {row.word_count} words</>}
      </div>
      <div style={{ fontSize: 14, marginTop: 4, fontWeight: 500 }}>
        {displayTitle}
      </div>
      {row.theme && (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)', marginTop: 4 }}>
          theme: {row.theme}
        </div>
      )}
      {row.body_preview && (
        <div
          style={{
            fontSize: 13, marginTop: 6,
            color: 'var(--fg-mute)',
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}
        >
          {row.body_preview}
        </div>
      )}
    </button>
  );
}
