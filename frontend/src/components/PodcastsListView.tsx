import { useEffect, useState } from 'react';
import { api } from '../api';
import type { PodcastListItem, View } from '../types';
import { Icon } from './icons';

interface Props {
  onNavigate: (view: View, id?: string) => void;
}

export function PodcastsListView({ onNavigate }: Props) {
  const [items, setItems] = useState<PodcastListItem[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.podcasts(100)
      .then((r) => setItems(r.items))
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'failed'));
  }, []);

  if (err) return <div className="podcasts-list-error">Couldn't load podcasts: {err}</div>;
  if (!items) return <div className="podcasts-list-loading">Loading…</div>;
  if (items.length === 0) {
    return (
      <div className="podcasts-list-empty">
        <h1 className="podcasts-list-title">Podcasts</h1>
        <p>
          No podcasts ingested yet. Send an RSS feed URL or YouTube link to{' '}
          <code>POST /api/listen</code> and it'll show up here once the Listener
          and Compiler finish chewing on it.
        </p>
      </div>
    );
  }

  return (
    <div className="podcasts-list">
      <header className="podcasts-list-header">
        <h1 className="podcasts-list-title">Podcasts</h1>
        <span className="podcasts-list-count">{items.length}</span>
      </header>
      <div className="podcasts-grid">
        {items.map((item) => (
          <button
            key={item.article_id}
            className="podcast-card"
            onClick={() => onNavigate('podcast', item.article_id)}
          >
            <div className="podcast-card-cover">
              {item.source_hero || item.article_hero ? (
                <img
                  src={item.source_hero || item.article_hero || ''}
                  alt={item.article_title}
                  loading="lazy"
                />
              ) : (
                <div className="podcast-card-fallback">
                  {item.source_kind === 'podcast' ? Icon.podcast : Icon.video}
                </div>
              )}
            </div>
            <div className="podcast-card-body">
              <div className="podcast-card-tag">
                {mediaLabel(item.source_kind)}
              </div>
              <h3 className="podcast-card-title">{item.article_title}</h3>
              {item.source_author && (
                <div className="podcast-card-show">{item.source_author}</div>
              )}
              <div className="podcast-card-meta">
                {item.source_published_at && (
                  <span>{formatDate(item.source_published_at)}</span>
                )}
                {item.source_duration_sec != null && (
                  <>
                    <span className="podcast-card-meta-dot">·</span>
                    <span>{formatDuration(item.source_duration_sec)}</span>
                  </>
                )}
              </div>
              {item.article_summary && (
                <p className="podcast-card-summary">
                  {item.article_summary.slice(0, 180)}
                  {item.article_summary.length > 180 && '…'}
                </p>
              )}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

function formatDuration(sec: number): string {
  const m = Math.round(sec / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm === 0 ? `${h}h` : `${h}h ${rm}m`;
}

function mediaLabel(kind: PodcastListItem['source_kind']): string {
  if (kind === 'youtube') return 'YouTube';
  if (kind === 'video') return 'Video';
  return 'Podcast';
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso.replace(' ', 'T'));
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  } catch {
    return iso.slice(0, 10);
  }
}
