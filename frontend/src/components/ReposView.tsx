import { useEffect, useState } from 'react';
import { api } from '../api';
import type { RepoSummary, View } from '../types';

interface Props {
  onNavigate: (view: View, id?: string) => void;
  onAddRepo: () => void;
  reloadKey?: number;
}

export function ReposView({ onNavigate, onAddRepo, reloadKey }: Props) {
  const [rows, setRows] = useState<RepoSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.repos.list()
      .then(setRows)
      .catch((e) => setErr(e instanceof Error ? e.message : 'failed'));
  }, [reloadKey]);

  if (err) return <div className="view"><p style={{ color: 'var(--danger, crimson)' }}>{err}</p></div>;
  if (!rows) return <div className="view"><p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading…</p></div>;

  const isEmpty = rows.length === 0;

  return (
    <div className="view">
      <div className="view-h">Repos</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <h1 className="view-title" style={{ margin: 0 }}>
          {rows.length} repo{rows.length === 1 ? '' : 's'}
        </h1>
        {!isEmpty && <button className="chip" onClick={onAddRepo}>+ add repo</button>}
      </div>

      {isEmpty && (
        <div style={{ marginTop: 8 }}>
          <p className="view-sub" style={{ marginBottom: 12 }}>
            Track a GitHub repo and mastisk will poll it hourly, build rolling context, and generate 4 idea-notes per repo per day.
          </p>
          <p className="view-sub" style={{ marginBottom: 16 }}>
            Try <code>anthropics/claude-code</code> — pulls the README + recent commits + open issues.
            Private repos need a GitHub PAT in <a
              href="#settings"
              onClick={(e) => { e.preventDefault(); onNavigate('settings'); }}
              style={{ color: 'var(--accent, #0a7)', textDecoration: 'underline', cursor: 'pointer' }}
            >Settings</a>.
          </p>
          <button
            onClick={onAddRepo}
            style={{
              padding: '10px 18px',
              fontSize: 14,
              fontWeight: 600,
              background: 'var(--accent, #0a7)',
              color: 'var(--bg, white)',
              border: '1px solid var(--accent, #0a7)',
              borderRadius: 6,
              cursor: 'pointer',
            }}
          >
            + add your first repo
          </button>
          <p className="view-sub" style={{ marginTop: 12, fontSize: 11 }}>
            Or run <code>mastisk add-repo owner/name</code> from the CLI.
          </p>
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
        {rows.map((r) => (
          <button
            key={r.slug}
            onClick={() => onNavigate('repo', r.slug)}
            style={{
              textAlign: 'left', padding: 10,
              border: '1px solid var(--border)', borderRadius: 6,
              background: 'var(--bg-soft, transparent)', cursor: 'pointer',
            }}
          >
            <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)' }}>
              {r.source_type === 'local'
                ? `📁 ${r.display_name ?? r.slug}`
                : r.slug}
              {r.snapshot?.stars_count !== null && r.snapshot?.stars_count !== undefined && (
                <> · ★ {r.snapshot.stars_count}</>
              )}
              {r.last_polled_at && <> · polled {new Date(r.last_polled_at).toLocaleString()}</>}
              {r.snapshot?.error && <> · <span style={{ color: 'var(--danger, crimson)' }}>{r.snapshot.error}</span></>}
            </div>
            {r.description && <div style={{ fontSize: 13, marginTop: 4 }}>{r.description}</div>}
          </button>
        ))}
      </div>
    </div>
  );
}
