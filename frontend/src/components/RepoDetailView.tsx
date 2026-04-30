import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { RepoDetail, RepoIdeasResponse, View } from '../types';

interface Props {
  slug: string;
  onNavigate: (view: View, id?: string) => void;
}

export function RepoDetailView({ slug, onNavigate }: Props) {
  const [repo, setRepo] = useState<RepoDetail | null>(null);
  const [ideas, setIdeas] = useState<RepoIdeasResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    setRepo(null);
    setIdeas(null);
    api.repos.get(slug)
      .then(setRepo)
      .catch((e) => setErr(e instanceof Error ? e.message : 'failed'));
    api.repos.ideas(slug)
      .then(setIdeas)
      .catch(() => { /* non-fatal — detail view still renders */ });
  }, [slug]);
  useEffect(load, [load]);

  const onPoll = async () => {
    setBusy('poll');
    try { await api.repos.pollNow(slug); setTimeout(load, 1500); }
    catch (e) { setErr(e instanceof Error ? e.message : 'poll failed'); }
    finally { setBusy(null); }
  };
  const onIdeate = async () => {
    setBusy('ideate');
    try { await api.repos.ideateNow(slug); setTimeout(load, 1500); }
    catch (e) { setErr(e instanceof Error ? e.message : 'ideate failed'); }
    finally { setBusy(null); }
  };
  const onDelete = async () => {
    if (!confirm(`Stop tracking ${slug}? Historical snapshots and generated notes are kept.`)) return;
    try {
      await api.repos.delete(slug);
      onNavigate('repos');
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'delete failed');
    }
  };

  if (err) return <div className="view"><p style={{ color: 'var(--danger, crimson)' }}>{err}</p></div>;
  if (!repo) return <div className="view"><p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading…</p></div>;

  return (
    <div className="view">
      <div className="view-h">Repo</div>
      <h1 className="view-title">{repo.display_name ?? repo.slug}</h1>
      {repo.source_type === 'local' ? (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--fg-faint)', marginBottom: 8 }}>
          local · <code>{repo.local_path}</code>
        </div>
      ) : (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--fg-faint)', marginBottom: 8 }}>
          github · <a href={`https://github.com/${repo.slug}`} target="_blank" rel="noreferrer">{repo.slug}</a>
        </div>
      )}
      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)', marginBottom: 12 }}>
        {repo.last_polled_at && <>polled {new Date(repo.last_polled_at).toLocaleString()}</>}
        {repo.last_ideated_at && <> · ideated {new Date(repo.last_ideated_at).toLocaleString()}</>}
        {repo.is_private && <> · <strong>private</strong></>}
      </div>

      {repo.description && <p>{repo.description}</p>}

      <section style={{ marginTop: 16 }}>
        <h3 style={{ fontSize: 13, color: 'var(--fg-faint)', marginBottom: 8, fontFamily: 'var(--mono)' }}>
          Rolling context
        </h3>
        {repo.context_md ? (
          <pre style={{
            whiteSpace: 'pre-wrap', fontSize: 13, lineHeight: 1.5,
            background: 'var(--bg-soft, transparent)',
            border: '1px solid var(--border)', borderRadius: 6, padding: 12,
          }}>
            {repo.context_md}
          </pre>
        ) : (
          <p style={{ color: 'var(--fg-faint)', fontSize: 12 }}>
            Not yet polled. Hit "poll now" to trigger immediately.
          </p>
        )}
      </section>

      <IdeasSection ideas={ideas} onNavigate={onNavigate} />

      <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
        <button onClick={() => onNavigate('repos')}>← all repos</button>
        <button onClick={onPoll} disabled={busy === 'poll'}>
          {busy === 'poll' ? 'polling…' : 'poll now'}
        </button>
        <button onClick={onIdeate} disabled={busy === 'ideate'}>
          {busy === 'ideate' ? 'ideating…' : 'ideate now'}
        </button>
        <button onClick={onDelete} style={{ marginLeft: 'auto' }}>remove</button>
      </div>
    </div>
  );
}

function IdeasSection({
  ideas,
  onNavigate,
}: {
  ideas: RepoIdeasResponse | null;
  onNavigate: (view: View, id?: string) => void;
}) {
  const heading = (
    <h3 style={{ fontSize: 13, color: 'var(--fg-faint)', marginBottom: 8, fontFamily: 'var(--mono)' }}>
      Ideas from this repo{ideas && ` · ${ideas.ideas.length}`}
    </h3>
  );

  if (!ideas) {
    return (
      <section style={{ marginTop: 24 }}>
        {heading}
        <p style={{ color: 'var(--fg-faint)', fontSize: 12, fontFamily: 'var(--mono)' }}>loading…</p>
      </section>
    );
  }

  // Only surface the banner when the *actual* last run errored. ``find`` would
  // keep showing a stale failure forever if any of the last 5 runs had errored
  // — even after later runs succeeded — which made "last run errored" lie.
  const lastFailed = ideas.runs[0]?.error ? ideas.runs[0] : null;

  return (
    <section style={{ marginTop: 24 }}>
      {heading}

      {lastFailed && (
        <div style={{
          fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--danger, crimson)',
          border: '1px solid var(--danger, crimson)', borderRadius: 6,
          padding: '6px 10px', marginBottom: 10,
        }}>
          last run errored @ {new Date(lastFailed.ideated_at).toLocaleString()}: {lastFailed.error}
        </div>
      )}

      {ideas.ideas.length === 0 ? (
        <p style={{ color: 'var(--fg-faint)', fontSize: 12, lineHeight: 1.5 }}>
          No ideas yet. Ideas are generated from the rolling context on each ideation run and
          flow through the Notetaker — hit "ideate now" to trigger one immediately, or wait for
          the scheduled run.
        </p>
      ) : (
        <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {ideas.ideas.map((n) => (
            <li key={n.id}>
              <a
                href={`/notes/${n.id}`}
                onClick={(e) => { e.preventDefault(); onNavigate('note', String(n.id)); }}
                style={{
                  display: 'flex', alignItems: 'baseline', gap: 10,
                  fontSize: 13, lineHeight: 1.5, color: 'var(--fg)',
                  textDecoration: 'none',
                  padding: '6px 0', borderBottom: '1px solid var(--line-soft)',
                }}
              >
                <span style={{
                  fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--fg-faint)',
                  minWidth: 60, textAlign: 'right',
                }}>
                  {new Date(n.created_at).toLocaleDateString()}
                </span>
                {n.classification && (
                  <span style={{
                    fontFamily: 'var(--mono)', fontSize: 10,
                    color: 'var(--fg-mute)', textTransform: 'lowercase',
                    border: '1px solid var(--line-soft)', borderRadius: 4,
                    padding: '0 4px',
                  }}>
                    {n.classification}
                  </span>
                )}
                <span style={{ flex: 1 }}>{n.summary ?? n.slug}</span>
              </a>
            </li>
          ))}
        </ul>
      )}

      {ideas.runs.length > 0 && (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--fg-faint)', marginTop: 8 }}>
          {ideas.runs.length} run{ideas.runs.length === 1 ? '' : 's'} tracked
          {' · '}
          last: {new Date(ideas.runs[0].ideated_at).toLocaleString()}
          {ideas.runs[0].model && ` · model=${ideas.runs[0].model}`}
        </div>
      )}
    </section>
  );
}
