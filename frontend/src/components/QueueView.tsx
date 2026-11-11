import type React from 'react';
import { useEffect, useState } from 'react';
import { api } from '../api';
import type { Job, View } from '../types';
import '../styles/views/QueueView.css';

interface Props {
  onNavigate?: (view: View, id?: string) => void;
}

export function QueueView({ onNavigate }: Props = {}) {
  const [jobs, setJobs] = useState<Job[] | null>(null);

  useEffect(() => {
    const tick = () => api.jobs().then((d) => setJobs(d.jobs)).catch(() => {});
    void tick();
    const id = setInterval(tick, 5000);
    return () => clearInterval(id);
  }, []);

  if (!jobs) return <div className="view"><p style={{color:'var(--fg-faint)'}}>loading…</p></div>;

  const by: Record<string, Job[]> = { queued: [], running: [], done: [], failed: [] };
  for (const j of jobs) (by[j.status] ?? by.queued).push(j);

  return (
    <div className="view">
      <div className="view-h">Today · Reading queue</div>
      <h1 className="view-title">What the agents are working on.</h1>
      <p className="view-sub">
        Scout clips items into sources; Compiler picks each up and turns it into a wiki page.
        Failed jobs stay here until you clear or retry them.
      </p>

      <div className="counters">
        <Counter n={by.queued.length} label="Queued" />
        <Counter n={by.running.length} label="Running" variant={by.running.length > 0 ? 'accent' : undefined} />
        <Counter n={by.done.length} label="Done" />
        <Counter n={by.failed.length} label="Failed" variant={by.failed.length > 0 ? 'alert' : undefined} />
      </div>

      {jobs.length === 0 ? (
        <div style={{padding:'24px',border:'1px dashed var(--line)',borderRadius:8,fontFamily:'var(--serif)',color:'var(--fg-mute)'}}>
          No jobs yet. Subscribe a feed or queue a YouTube URL to give the agents something to do.
        </div>
      ) : (
        <div className="queue-list">
          {jobs.map((j) => <QueueRow key={j.id} job={j} onNavigate={onNavigate}/>)}
        </div>
      )}
    </div>
  );
}

function Counter({ n, label, variant }: { n: number; label: string; variant?: 'accent' | 'alert' }) {
  const cls = ['counter'];
  if (n === 0) cls.push('is-empty');
  if (variant === 'accent') cls.push('is-accent');
  if (variant === 'alert') cls.push('is-alert');
  return <div className={cls.join(' ')}><div className="v">{n}</div><div className="l">{label}</div></div>;
}

function QueueRow({ job, onNavigate }: { job: Job; onNavigate?: (view: View, id?: string) => void }) {
  const rawTitle = job.detail.title;
  const title = rawTitle ?? fallbackTitle(job);
  const isFallback = !rawTitle;
  const subtitle = job.detail.subtitle;
  const srcKind = job.detail.source_kind;
  const articleId = job.detail.article_id;
  const clickable = !!(articleId && onNavigate);

  const onClick = clickable ? () => onNavigate!('article', articleId!) : undefined;
  const onKeyDown = clickable
    ? (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onNavigate!('article', articleId!); }
      }
    : undefined;

  return (
    <div
      className={`queue-row s-${job.status}${clickable ? ' is-clickable' : ''}`}
      onClick={onClick}
      onKeyDown={onKeyDown}
      role={clickable ? 'link' : undefined}
      tabIndex={clickable ? 0 : undefined}
    >
      <div className="queue-id">#{job.id}</div>
      <div className="queue-title">
        <div className={`queue-title-line${isFallback ? ' is-fallback' : ''}`} title={title}>{title}</div>
        <div className="queue-meta">
          <span className="queue-agent">{job.agent}</span>
          <span className="queue-sep">·</span>
          <span>{job.kind}</span>
          {srcKind && <><span className="queue-sep">·</span><span>{srcKind}</span></>}
          {subtitle && <><span className="queue-sep">·</span><span className="queue-host">{subtitle}</span></>}
        </div>
      </div>
      <div className="queue-end">
        <span className={`queue-status s-${job.status}`}>
          {job.status}
          {job.attempts > 1 && <span className="retry">· retry {job.attempts}</span>}
        </span>
        <span className="queue-time">{relativeTs(job.created_at)}</span>
      </div>
      {job.error && (
        <div className="queue-error" title={job.error}>
          {job.error.length > 200 ? `${job.error.slice(0, 200)}…` : job.error}
        </div>
      )}
    </div>
  );
}

function fallbackTitle(j: Job): string {
  if (j.agent === 'scout') return 'Polling feeds';
  return 'Untitled task';
}

function relativeTs(iso: string): string {
  const delta = Date.now() - new Date(iso.replace(' ', 'T') + 'Z').getTime();
  const m = Math.floor(delta / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h/24)}d ago`;
}
