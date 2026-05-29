import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { TweetThread, View } from '../types';

interface Props {
  onNavigate: (view: View, id?: string) => void;
}

const WINDOW_CHOICES = [1, 3, 7, 14, 30] as const;
const PAGE_SIZE = 50;

export function TweetThreadsView({ onNavigate }: Props) {
  const [rows, setRows] = useState<TweetThread[] | null>(null);
  const [theme, setTheme] = useState('');
  const [url, setUrl] = useState('');
  const [windowDays, setWindowDays] = useState<(typeof WINDOW_CHOICES)[number]>(7);
  const [includeWeb, setIncludeWeb] = useState(true);
  const [useBrowser, setUseBrowser] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api.tweets.list({ limit: PAGE_SIZE })
      .then(setRows)
      .catch((e) => setErr(e instanceof Error ? e.message : 'failed'));
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const submit = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      const created = await api.tweets.create({
        theme: theme.trim(),
        url: url.trim(),
        window_days: windowDays,
        include_web: includeWeb,
        use_browser_context: useBrowser,
      });
      onNavigate('tweet_thread', String(created.id));
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed');
      setBusy(false);
    }
  }, [theme, url, windowDays, includeWeb, useBrowser, onNavigate]);

  return (
    <div className="view">
      <div className="view-h">Tweet Threads</div>
      <h1 className="view-title">Draft a timely thread</h1>
      <p className="view-sub">
        Uses recent Mastisk notes/articles, optional live web search, and optional browser context for an X URL or the current Chrome tab.
      </p>

      <div style={{ display: 'grid', gap: 10, maxWidth: 760, marginTop: 16 }}>
        <textarea
          value={theme}
          onChange={(e) => setTheme(e.target.value)}
          placeholder="Optional theme or observation to anchor the thread"
          rows={3}
          maxLength={500}
          style={{
            width: '100%', boxSizing: 'border-box', padding: 10,
            border: '1px solid var(--line)', borderRadius: 6,
            background: 'transparent', color: 'var(--fg)',
            fontFamily: 'var(--mono)', fontSize: 13,
          }}
        />
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="Optional URL or X post URL"
          style={{
            width: '100%', boxSizing: 'border-box', padding: 9,
            border: '1px solid var(--line)', borderRadius: 6,
            background: 'transparent', color: 'var(--fg)',
            fontFamily: 'var(--mono)', fontSize: 13,
          }}
        />
        <div
          role="radiogroup"
          aria-label="Source window in days"
          style={{ display: 'flex', border: '1px solid var(--line)', borderRadius: 6, overflow: 'hidden' }}
        >
          {WINDOW_CHOICES.map((n) => (
            <button
              key={n}
              type="button"
              role="radio"
              aria-checked={windowDays === n}
              onClick={() => setWindowDays(n)}
              style={{
                flex: 1, border: 'none', padding: '7px 8px',
                background: windowDays === n ? 'var(--fg)' : 'transparent',
                color: windowDays === n ? 'var(--bg)' : 'var(--fg)',
                fontFamily: 'var(--mono)', fontSize: 12,
              }}
            >
              {n}d
            </button>
          ))}
        </div>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
          <input type="checkbox" checked={includeWeb} onChange={(e) => setIncludeWeb(e.target.checked)} />
          Search recent web context
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
          <input type="checkbox" checked={useBrowser} onChange={(e) => setUseBrowser(e.target.checked)} />
          Use authenticated Chrome for URL/current tab
        </label>
        {err && <div style={{ color: 'var(--danger, crimson)', fontSize: 12 }}>{err}</div>}
        <div>
          <button onClick={submit} disabled={busy}>
            {busy ? 'queueing...' : 'Generate thread'}
          </button>
        </div>
      </div>

      <div style={{ marginTop: 28, display: 'flex', flexDirection: 'column', gap: 8 }}>
        {!rows && <p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading...</p>}
        {rows?.map((row) => (
          <button
            key={row.id}
            onClick={() => onNavigate('tweet_thread', String(row.id))}
            style={{
              textAlign: 'left', padding: 10, border: '1px solid var(--line)',
              borderRadius: 6, background: 'var(--bg-elev, transparent)', cursor: 'pointer',
            }}
          >
            <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)' }}>
              #{row.id} · {new Date(row.created_at).toLocaleString()} · {row.window_days}d · {row.status}
              {row.model && <> · {row.model}</>}
            </div>
            <div style={{ marginTop: 4, fontWeight: 500 }}>
              {row.title || (row.status === 'failed' ? 'Thread failed' : 'Thread in progress')}
            </div>
            {row.angle && (
              <div style={{ marginTop: 5, fontSize: 13, color: 'var(--fg-mute)' }}>{row.angle}</div>
            )}
          </button>
        ))}
      </div>
    </div>
  );
}
