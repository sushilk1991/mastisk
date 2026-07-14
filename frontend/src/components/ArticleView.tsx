import { useCallback, useEffect, useRef, useState } from 'react';
import type { Article, View } from '../types';
import { Icon } from './icons';
import { api } from '../api';
import { SynthesisFeedback } from './SynthesisFeedback';
import { NoteCaptureModal, type CaptureContext } from './NoteCaptureModal';
import { MermaidBlock } from './MermaidBlock';

interface Props {
  article: Article;
  onAsk: (prompt: string, selection: string | null) => void;
  onNavigate: (view: View, id?: string) => void;
}

// Dated-facts convention: fact bullets open with "(YYYY-MM-DD)" and may carry
// a "(previously X as of YYYY-MM-DD)" supersession clause. Wrap both in spans
// so the CSS can render them as quiet metadata instead of body prose.
export function decorateFactDates(html: string): string {
  return html
    .replace(
      /(<li[^>]*>)\s*\((\d{4}-\d{2}-\d{2})\)\s*/g,
      '$1<span class="fact-date">$2</span> ',
    )
    .replace(
      /\((previously\s[^()]*?\sas of\s\d{4}-\d{2}-\d{2})\)/gi,
      '<span class="fact-prev">($1)</span>',
    );
}

interface Pop { x: number; y: number; text: string; }

export function ArticleView({ article, onAsk, onNavigate }: Props) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const [pop, setPop] = useState<Pop | null>(null);
  const [captureCtx, setCaptureCtx] = useState<CaptureContext | null>(null);
  const readStartRef = useRef<number>(Date.now());
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null);
  const [verdict, setVerdict] = useState<'liked' | 'disliked' | null>(null);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [reason, setReason] = useState('');

  // Restore a prior thumbs verdict so votes survive remounts.
  useEffect(() => {
    let live = true;
    setVerdict(null); setReasonOpen(false); setReason('');
    api.signalVerdict(article.id)
      .then((d) => { if (live) setVerdict(d.verdict); })
      .catch(() => {});
    return () => { live = false; };
  }, [article.id]);

  function vote(kind: 'liked' | 'disliked') {
    setVerdict(kind);
    if (kind === 'disliked') {
      setReasonOpen(true);
    } else {
      setReasonOpen(false);
      api.signal(kind, article.id);
    }
  }

  function submitDislike() {
    api.signal('disliked', article.id, reason.trim() ? { reason: reason.trim() } : undefined);
    setReasonOpen(false);
  }

  const mediaLen = article.media?.length ?? 0;
  const closeLightbox = useCallback(() => setLightboxIdx(null), []);
  const prevMedia = useCallback(() => setLightboxIdx(i => i !== null ? (i - 1 + mediaLen) % mediaLen : null), [mediaLen]);
  const nextMedia = useCallback(() => setLightboxIdx(i => i !== null ? (i + 1) % mediaLen : null), [mediaLen]);

  useEffect(() => {
    if (lightboxIdx === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeLightbox();
      else if (e.key === 'ArrowLeft') prevMedia();
      else if (e.key === 'ArrowRight') nextMedia();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [lightboxIdx, closeLightbox, prevMedia, nextMedia]);

  // Emit "opened" signal on mount; "time_read" on unmount.
  useEffect(() => {
    readStartRef.current = Date.now();
    api.signal('opened', article.id);
    return () => {
      const seconds = Math.round((Date.now() - readStartRef.current) / 1000);
      if (seconds >= 2) api.signal('time_read', article.id, { seconds });
    };
  }, [article.id]);

  // Selection handler: desktop uses mouseup, mobile uses touchend + selectionchange
  useEffect(() => {
    const placePop = (text: string, rect: DOMRect) => {
      const main = document.querySelector('.main') as HTMLElement;
      if (!main) { setPop(null); return; }
      const m = main.getBoundingClientRect();
      setPop({
        x: rect.left + rect.width / 2 - m.left + main.scrollLeft,
        y: rect.top - m.top + main.scrollTop,
        text,
      });
    };
    const check = () => {
      const sel = window.getSelection();
      const text = sel?.toString().trim();
      if (!text || !bodyRef.current?.contains(sel?.anchorNode || null)) {
        setPop(null); return;
      }
      const range = sel!.getRangeAt(0);
      placePop(text, range.getBoundingClientRect());
    };
    const up = () => setTimeout(check, 0);
    document.addEventListener('mouseup', up);
    document.addEventListener('touchend', up);
    return () => {
      document.removeEventListener('mouseup', up);
      document.removeEventListener('touchend', up);
    };
  }, []);

  // Wiki-link clicks inside article body (delegate)
  useEffect(() => {
    const node = bodyRef.current;
    if (!node) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      const linkEl = t.closest('.link') as HTMLElement | null;
      if (!linkEl) return;
      const target = linkEl.getAttribute('data-target');
      if (target) {
        e.preventDefault();
        onNavigate('article', target);
      }
    };
    node.addEventListener('click', onClick);
    return () => node.removeEventListener('click', onClick);
  }, [onNavigate]);

  return (
    <div className="main-inner">
      <div className="art-meta">
        <span className={`art-kind ${article.kind.toLowerCase()}`}>
          <span className="k-glyph"/>{article.kind}
        </span>
        <span className="sep">·</span>
        <span>{article.updated_by ? `updated by ${article.updated_by}` : 'updated'}</span>
        <span className="sep">·</span>
        <span>{article.readingTime} read</span>
        <span className="sep">·</span>
        <span className="conf">conf
          <span className="conf-bar"><span className="fill" style={{width:`${article.confidence*100}%`}}/></span>
          {Math.round(article.confidence*100)}%
        </span>
        <span className="art-verdict">
          <button
            type="button"
            className={`verdict-btn ${verdict === 'liked' ? 'active' : ''}`}
            title="More like this"
            aria-label="More like this"
            aria-pressed={verdict === 'liked'}
            onClick={() => vote('liked')}
          >
            ▲
          </button>
          <button
            type="button"
            className={`verdict-btn down ${verdict === 'disliked' ? 'active' : ''}`}
            title="Less like this"
            aria-label="Less like this"
            aria-pressed={verdict === 'disliked'}
            onClick={() => vote('disliked')}
          >
            ▼
          </button>
        </span>
      </div>
      {reasonOpen && (
        <div className="verdict-reason">
          <input
            type="text"
            value={reason}
            maxLength={200}
            placeholder="why? (optional — teaches your agents)"
            aria-label="Why didn't this land? Optional — teaches your agents"
            onChange={(e) => setReason(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submitDislike(); }}
            autoFocus
          />
          <button type="button" className="chip" onClick={submitDislike}>save</button>
        </div>
      )}

      <h1 className="art-title">{article.title}</h1>
      {article.aka.length > 0 && (
        <div className="art-aka">
          <span className="label">also known as</span>
          {article.aka.map((a, i) => <span key={i} className="alias">{a}{i < article.aka.length-1 ? ' ·' : ''}</span>)}
        </div>
      )}
      {article.sourceList.length === 1 && article.sourceList[0].url && (
        <div className="art-source-pin">
          <span className="label">source</span>
          <a href={article.sourceList[0].url} target="_blank" rel="noopener noreferrer" className="art-source-link">
            {hostOf(article.sourceList[0].url)}
          </a>
        </div>
      )}

      {article.kind === 'Synthesis' && <SynthesisFeedback articleId={article.id}/>}

      {article.heroImageUrl && (
        <figure className="art-hero">
          <img
            src={article.heroImageUrl}
            alt={`${article.title} hero`}
            loading="lazy"
            onError={(e) => {
              // Hide broken hero images rather than showing the browser's
              // default "missing image" glyph above the title.
              (e.currentTarget.parentElement as HTMLElement | null)?.remove();
            }}
          />
        </figure>
      )}

      <p className="art-summary" dangerouslySetInnerHTML={{ __html: article.summary }}/>

      <div className="art-stats">
        <div className="art-stat"><div className="v">{article.sources}</div><div className="l">Sources</div></div>
        <div className="art-stat"><div className="v">{article.backlinks}</div><div className="l">Backlinks</div></div>
        <div className="art-stat"><div className="v">{article.forwardlinks}</div><div className="l">Forward links</div></div>
        <div className="art-stat"><div className="v">{article.related.length}</div><div className="l">Related concepts</div></div>
      </div>

      <div className="art-body" ref={bodyRef}>
        {article.sections.map((s, i) => {
          // sec-body is a div, not <p>: section bodies may contain multiple
          // <p> elements and nesting p-in-p is invalid HTML.
          if (s.kind === 'callout') return (
            <div key={i} className="callout">
              <h2>{s.h}</h2>
              <div className="sec-body" dangerouslySetInnerHTML={{ __html: decorateFactDates(s.body) }}/>
            </div>
          );
          if (s.kind === 'diagram') return (
            <div key={i} className="art-diagram">
              <h2>{s.h}</h2>
              <MermaidBlock source={s.body} />
            </div>
          );
          if (s.kind === 'open') return (
            <div key={i} className="open">
              <h2>{s.h}</h2>
              <div className="sec-body" dangerouslySetInnerHTML={{ __html: s.body }}/>
              <div style={{ marginTop: 14, display: 'flex', justifyContent: 'flex-end' }}>
                <button
                  type="button"
                  style={{
                    fontSize: 11, padding: '4px 10px',
                    border: '1px solid var(--border)', borderRadius: 4,
                    background: 'var(--bg-soft, transparent)', cursor: 'pointer',
                    fontFamily: 'var(--mono)',
                  }}
                  onClick={() => setCaptureCtx({
                    article_id: article.id,
                    section_heading: s.h,
                    question_html: s.body,
                  })}
                >
                  + add thoughts
                </button>
              </div>
            </div>
          );
          return (
            <div key={i}>
              <h2>{s.h}</h2>
              <div className="sec-body" dangerouslySetInnerHTML={{ __html: decorateFactDates(s.body) }}/>
            </div>
          );
        })}
      </div>

      {article.media && article.media.length > 0 && (
        <div className="art-media">
          <h3>From the source</h3>
          <div className="art-media-grid">
            {article.media.map((m, i) => (
              <figure key={`${m.src}-${i}`} className="art-media-item" onClick={() => setLightboxIdx(i)} style={{ cursor: 'pointer' }}>
                <img
                  src={m.src}
                  alt={m.alt || ''}
                  loading="lazy"
                  onError={(e) => {
                    (e.currentTarget.parentElement as HTMLElement | null)?.remove();
                  }}
                />
                {(m.caption || m.alt) && (
                  <figcaption>{m.caption || m.alt}</figcaption>
                )}
              </figure>
            ))}
          </div>
        </div>
      )}

      {lightboxIdx !== null && article.media && (
        <div className="lightbox-overlay" onClick={closeLightbox}>
          <div className="lightbox-content" onClick={(e) => e.stopPropagation()}>
            <img src={article.media[lightboxIdx].src} alt={article.media[lightboxIdx].alt || ''} />
            {(article.media[lightboxIdx].caption || article.media[lightboxIdx].alt) && (
              <p className="lightbox-caption">{article.media[lightboxIdx].caption || article.media[lightboxIdx].alt}</p>
            )}
          </div>
          {mediaLen > 1 && (
            <>
              <button className="lightbox-nav lightbox-prev" onClick={(e) => { e.stopPropagation(); prevMedia(); }} aria-label="Previous">&#8249;</button>
              <button className="lightbox-nav lightbox-next" onClick={(e) => { e.stopPropagation(); nextMedia(); }} aria-label="Next">&#8250;</button>
            </>
          )}
          <button className="lightbox-close" onClick={closeLightbox} aria-label="Close">&times;</button>
        </div>
      )}

      {article.sourceList.length > 0 && (
        <div className="art-sources">
          <h3>Sources used in this page</h3>
          {article.sourceList.map((s, i) => (
            <div key={i} className="src-row">
              <div className="src-kind">{s.kind}</div>
              {s.url ? (
                <a className="src-title src-link" href={s.url} target="_blank" rel="noopener noreferrer">
                  {s.title || s.url}
                </a>
              ) : (
                <div className="src-title">{s.title}</div>
              )}
              <div className="src-date">{s.date}</div>
            </div>
          ))}
        </div>
      )}

      {pop && (
        <div className="ask-pop" style={{ left: pop.x, top: pop.y }} onMouseDown={(e) => e.preventDefault()}>
          <button onClick={() => { onAsk(`What does "${pop.text}" mean here?`, pop.text); setPop(null); }}>
            {Icon.ask} Ask
          </button>
          <div className="sep"/>
          <button onClick={() => { onAsk(`Expand on: ${pop.text}`, pop.text); setPop(null); }}>
            {Icon.expand} Expand
          </button>
          <div className="sep"/>
          <button onClick={() => setPop(null)}>
            {Icon.link} Find related
          </button>
        </div>
      )}

      <NoteCaptureModal
        open={captureCtx !== null}
        onClose={() => setCaptureCtx(null)}
        onCaptured={() => setCaptureCtx(null)}
        context={captureCtx ?? undefined}
      />
    </div>
  );
}

function hostOf(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; }
}
