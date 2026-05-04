import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import AudioPlayer from 'react-h5-audio-player';
import 'react-h5-audio-player/lib/styles.css';
import { api, ApiError } from '../api';
import type { PodcastView as PodcastViewT, View, TranscriptAnchor, TranscriptSegment } from '../types';
import { Icon } from './icons';
import { NoteCaptureModal, type CaptureContext } from './NoteCaptureModal';

interface Props {
  articleId: string;
  onAsk: (prompt: string, selection: string | null) => void;
  onNavigate: (view: View, id?: string) => void;
}

export function PodcastView({ articleId, onAsk, onNavigate }: Props) {
  const [data, setData] = useState<PodcastViewT | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const [captureCtx, setCaptureCtx] = useState<CaptureContext | null>(null);
  const [pendingAnchor, setPendingAnchor] = useState<TranscriptAnchor | null>(null);
  const [activeSegmentIdx, setActiveSegmentIdx] = useState<number | null>(null);
  const audioRef = useRef<AudioPlayer | null>(null);

  const load = useCallback(() => {
    setErr(null);
    api.podcast(articleId)
      .then((view) => {
        setData(view);
        // If segments are present, the transcript is meaningful enough to open
        // by default — that's the "full experience" path. Otherwise stay
        // collapsed so the takeaways breathe (Phase 1 default).
        if (view.segments.length > 0) setTranscriptOpen(true);
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 404) {
          // Not a podcast article — bounce to the regular ArticleView.
          onNavigate('article', articleId);
          return;
        }
        setErr(e instanceof Error ? e.message : 'failed to load podcast');
      });
  }, [articleId, onNavigate]);

  useEffect(() => { load(); }, [load]);

  // Map segment idx → DOM ref so we can scroll the active one into view as
  // the audio progresses. Phase 2-deferred sync would use this; today we
  // only highlight on click. Held in a ref to avoid re-allocating each render.
  const segRefs = useRef<Record<number, HTMLLIElement | null>>({});

  const seekTo = useCallback((seg: TranscriptSegment) => {
    setActiveSegmentIdx(seg.idx);
    const el = audioRef.current?.audio?.current;
    if (el) {
      el.currentTime = seg.start_sec;
      // Don't auto-play — let the user decide. Click = navigation, not commit.
    }
  }, []);

  const captureForSegment = useCallback((seg: TranscriptSegment) => {
    if (!data) return;
    setPendingAnchor({
      source_id: data.source.id,
      segment_idx: seg.idx,
      start_sec: seg.start_sec,
    });
    setCaptureCtx({
      article_id: articleId,
      section_heading: `Transcript @ ${formatTime(seg.start_sec)}`,
      question_html: seg.text,
    });
  }, [articleId, data]);

  const onCaptured = useCallback(() => {
    setCaptureCtx(null);
    setPendingAnchor(null);
    // Refetch so the freshly-captured note shows up under its segment.
    load();
  }, [load]);

  const notesByIdx = useMemo(() => {
    if (!data) return new Map<number, PodcastViewT['anchored_notes']>();
    const m = new Map<number, PodcastViewT['anchored_notes']>();
    for (const n of data.anchored_notes) {
      const idx = n.transcript_anchor.segment_idx;
      const arr = m.get(idx) ?? [];
      arr.push(n);
      m.set(idx, arr);
    }
    return m;
  }, [data]);

  if (err) return <div className="podcast-error">Couldn't load podcast: {err}</div>;
  if (!data) return <div className="podcast-loading">Loading podcast…</div>;

  const cover = data.source.hero_image_url || data.article.heroImageUrl || null;
  const isYouTube = data.source.kind === 'youtube';

  return (
    <div className="podcast-view">
      <header className="pod-header">
        <div className="pod-cover">
          {cover ? (
            <img src={cover} alt={data.source.title || data.article.title} />
          ) : (
            <div className="pod-cover-fallback">
              {isYouTube ? Icon.video : Icon.podcast}
            </div>
          )}
        </div>
        <div className="pod-meta">
          <div className="pod-kind-tag">{isYouTube ? 'YouTube' : 'Podcast'}</div>
          <h1 className="pod-title">{data.article.title}</h1>
          {data.source.author && (
            <div className="pod-show">{data.source.author}</div>
          )}
          <div className="pod-meta-row">
            {data.source.published_at && (
              <span>{formatDate(data.source.published_at)}</span>
            )}
            {data.source.duration_sec != null && (
              <>
                <span className="pod-meta-dot">·</span>
                <span>{formatDuration(data.source.duration_sec)}</span>
              </>
            )}
            {data.source.url && (
              <>
                <span className="pod-meta-dot">·</span>
                <a className="pod-source-link" href={data.source.url}
                   target="_blank" rel="noopener noreferrer">
                  source ↗
                </a>
              </>
            )}
          </div>
          {data.article.summary && (
            <p className="pod-summary">{data.article.summary}</p>
          )}
        </div>
      </header>

      {!isYouTube && data.source.url && (
        <div className="pod-player">
          <AudioPlayer
            ref={audioRef}
            src={data.source.url}
            showJumpControls
            showFilledProgress
            showSkipControls={false}
            customAdditionalControls={[]}
            layout="horizontal"
            progressJumpSteps={{ forward: 30000, backward: 15000 }}
            customVolumeControls={[]}
          />
        </div>
      )}

      <section className="pod-takeaways">
        <h2 className="pod-section-h">Key takeaways</h2>
        {data.article.sections.length === 0 ? (
          <p className="pod-empty">
            No structured takeaways yet — the Compiler is still digesting this episode.
          </p>
        ) : (
          data.article.sections.map((s) => (
            <div key={s.idx} className={`pod-takeaway pod-takeaway-${s.kind}`}>
              <h3>{s.h}</h3>
              <div dangerouslySetInnerHTML={{ __html: s.body }} />
            </div>
          ))
        )}
      </section>

      <section className="pod-transcript-section">
        <button
          className="pod-transcript-toggle"
          onClick={() => setTranscriptOpen((v) => !v)}
          aria-expanded={transcriptOpen}
        >
          <span>{transcriptOpen ? '▼' : '▶'}</span>
          Transcript
          <span className="pod-transcript-meta">
            {data.segments.length > 0
              ? `${data.segments.length} segments`
              : `${Math.round(data.transcript_text.length / 1000)}k chars`}
          </span>
        </button>

        {transcriptOpen && (
          data.segments.length > 0 ? (
            <ol className="pod-transcript-segments">
              {data.segments.map((seg) => {
                const notes = notesByIdx.get(seg.idx) || [];
                const active = activeSegmentIdx === seg.idx;
                return (
                  <li
                    key={seg.idx}
                    className={`pod-segment ${active ? 'pod-segment-active' : ''}`}
                    ref={(el) => { segRefs.current[seg.idx] = el; }}
                  >
                    <div className="pod-segment-row">
                      <button
                        className="pod-segment-time"
                        onClick={() => seekTo(seg)}
                        title="Seek audio to this moment"
                      >
                        {formatTime(seg.start_sec)}
                      </button>
                      <div className="pod-segment-text" onClick={() => seekTo(seg)}>
                        {seg.text}
                      </div>
                      <button
                        className="pod-segment-note-btn"
                        onClick={() => captureForSegment(seg)}
                        title="Capture a note anchored to this moment"
                        aria-label="Add note"
                      >
                        +
                      </button>
                    </div>
                    {notes.length > 0 && (
                      <div className="pod-segment-notes">
                        {notes.map((n) => (
                          <div key={n.id} className="pod-segment-note">
                            <div className="pod-note-meta">
                              {n.classification && (
                                <span className="pod-note-class">{n.classification}</span>
                              )}
                              <span className="pod-note-time">
                                {formatDate(n.created_at)}
                              </span>
                            </div>
                            <div className="pod-note-body">
                              {(n.body || '').slice(0, 280)}
                              {(n.body || '').length > 280 && '…'}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          ) : (
            <pre className="pod-transcript-text">{data.transcript_text}</pre>
          )
        )}
      </section>

      <NoteCaptureModal
        open={captureCtx !== null}
        onClose={() => { setCaptureCtx(null); setPendingAnchor(null); }}
        onCaptured={onCaptured}
        context={captureCtx ?? undefined}
        transcriptAnchor={pendingAnchor}
      />
    </div>
  );
}

function formatTime(sec: number): string {
  const s = Math.floor(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
  return `${m}:${String(r).padStart(2, '0')}`;
}

function formatDuration(sec: number): string {
  const m = Math.round(sec / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm === 0 ? `${h}h` : `${h}h ${rm}m`;
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso.replace(' ', 'T'));
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  } catch {
    return iso.slice(0, 10);
  }
}
