import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { View, WikiSuggestion } from '../types';

interface Props {
  onNavigate: (view: View, id?: string) => void;
}

export function SuggestionsView({ onNavigate }: Props) {
  const [pending, setPending] = useState<WikiSuggestion[] | null>(null);
  const [dismissed, setDismissed] = useState<WikiSuggestion[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [showDismissed, setShowDismissed] = useState(false);

  const load = useCallback(() => {
    Promise.all([
      api.wikiSuggestions.list('pending'),
      api.wikiSuggestions.list('dismissed'),
    ])
      .then(([p, d]) => { setPending(p.suggestions); setDismissed(d.suggestions); })
      .catch((e: Error) => setErr(e.message));
  }, []);

  useEffect(() => { load(); }, [load]);

  async function decide(slug: string, action: 'promote' | 'dismiss' | 'restore') {
    setBusy(slug);
    try {
      await api.wikiSuggestions.decide(slug, action);
      load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  if (err) {
    return (
      <div className="view">
        <div className="view-h">Wiki · Suggestions</div>
        <p style={{color:'var(--fg-faint)',fontFamily:'var(--mono)',fontSize:12}}>
          couldn't load suggestions: {err}
        </p>
      </div>
    );
  }

  if (!pending) {
    return (
      <div className="view">
        <div className="view-h">Wiki · Suggestions</div>
        <p style={{color:'var(--fg-faint)',fontFamily:'var(--mono)',fontSize:12}}>loading…</p>
      </div>
    );
  }

  return (
    <div className="view">
      <div className="view-h">Wiki · Suggestions</div>
      <h1 className="view-title">
        {pending.length === 0 ? 'The queue is clear.' : 'Topics waiting for a page.'}
      </h1>
      <p className="view-sub">
        Wiki links whose target doesn't exist yet. Each becomes a page on its own
        once enough independent articles reference it — or right now, if you promote it.
        Dismissed topics keep counting but never auto-create.
      </p>

      <div className="sugg-list">
        {pending.map((s) => (
          <article key={s.slug} className="sugg-row">
            <div className="sugg-main">
              <div className="sugg-title">{s.title}</div>
              <div className="sugg-meta">
                <code className="sugg-slug">{s.slug}</code>
                <span className="sugg-count">
                  {s.occurrences} reference{s.occurrences === 1 ? '' : 's'}
                </span>
              </div>
              {s.referrers.length > 0 && (
                <div className="sugg-refs">
                  {s.referrers.slice(0, 4).map((r) => (
                    <button
                      key={r}
                      type="button"
                      className="sugg-ref"
                      onClick={() => onNavigate('article', r)}
                    >
                      {r}
                    </button>
                  ))}
                  {s.referrers.length > 4 && (
                    <span className="sugg-ref-more">+{s.referrers.length - 4} more</span>
                  )}
                </div>
              )}
            </div>
            <div className="sugg-actions">
              <button
                type="button"
                className="chip"
                disabled={busy === s.slug}
                onClick={() => void decide(s.slug, 'promote')}
              >
                promote
              </button>
              <button
                type="button"
                className="chip muted"
                disabled={busy === s.slug}
                onClick={() => void decide(s.slug, 'dismiss')}
              >
                dismiss
              </button>
            </div>
          </article>
        ))}
        {pending.length === 0 && (
          <p style={{color:'var(--fg-faint)',fontFamily:'var(--mono)',fontSize:12}}>
            New wiki-link targets from your agents' compiles will land here.
          </p>
        )}
      </div>

      {dismissed.length > 0 && (
        <div style={{marginTop: 32}}>
          <button
            type="button"
            className="chip muted"
            onClick={() => setShowDismissed(!showDismissed)}
          >
            {showDismissed ? 'hide' : 'show'} dismissed ({dismissed.length})
          </button>
          {showDismissed && (
            <div className="sugg-list" style={{marginTop: 12, opacity: 0.7}}>
              {dismissed.map((s) => (
                <article key={s.slug} className="sugg-row">
                  <div className="sugg-main">
                    <div className="sugg-title">{s.title}</div>
                    <div className="sugg-meta">
                      <code className="sugg-slug">{s.slug}</code>
                      <span className="sugg-count">
                        {s.occurrences} reference{s.occurrences === 1 ? '' : 's'}
                      </span>
                    </div>
                  </div>
                  <div className="sugg-actions">
                    <button
                      type="button"
                      className="chip"
                      disabled={busy === s.slug}
                      onClick={() => void decide(s.slug, 'restore')}
                    >
                      restore
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
