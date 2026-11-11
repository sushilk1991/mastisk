import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import { routeToPath } from '../router';
import type { OpenQuestion, View } from '../types';
import { NoteCaptureModal, type CaptureContext } from './NoteCaptureModal';
import { SafeHtml } from './SafeHtml';

interface Props {
  onNavigate: (view: View, id?: string) => void;
}

// Questions fetched and revealed per step. The full set is ~15k rows, so the
// view never asks for, or renders, more than the reader has scrolled to.
const PAGE_SIZE = 100;

interface Group {
  id: string;
  title: string;
  kind: string;
  updated_at: string;
  items: OpenQuestion[];
}

// Group by article so a reader sees all the threads under one page. Keyed on
// article id rather than adjacency, so an article whose questions straddle a
// page boundary stays one group once the next page arrives.
function groupByArticle(questions: OpenQuestion[]): Group[] {
  const groups: Group[] = [];
  const indexById = new Map<string, number>();
  for (const q of questions) {
    let i = indexById.get(q.article_id);
    if (i === undefined) {
      i = groups.length;
      indexById.set(q.article_id, i);
      groups.push({
        id: q.article_id,
        title: q.article_title,
        kind: q.article_kind,
        updated_at: q.updated_at,
        items: [],
      });
    }
    groups[i].items.push(q);
  }
  return groups;
}

export function OpenQuestionsView({ onNavigate }: Props) {
  const [questions, setQuestions] = useState<OpenQuestion[] | null>(null);
  const [totals, setTotals] = useState({ questions: 0, articles: 0 });
  const [err, setErr] = useState<string | null>(null);
  const [moreErr, setMoreErr] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [captureCtx, setCaptureCtx] = useState<CaptureContext | null>(null);

  const busyRef = useRef(false);
  const offsetRef = useRef(0);
  // Rows already in memory but not rendered yet. Only filled by a daemon that
  // predates paging and answers with the whole set at once.
  const backlogRef = useRef<OpenQuestion[]>([]);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  const loadMore = useCallback(async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setLoadingMore(true);
    setMoreErr(null);
    try {
      if (backlogRef.current.length > 0) {
        const next = backlogRef.current.slice(0, PAGE_SIZE);
        backlogRef.current = backlogRef.current.slice(PAGE_SIZE);
        setQuestions((prev) => [...(prev ?? []), ...next]);
        return;
      }
      const offset = offsetRef.current;
      const d = await api.openQuestions(PAGE_SIZE, offset);
      if (typeof d.total !== 'number') {
        backlogRef.current = d.questions.slice(PAGE_SIZE);
        setTotals({
          questions: d.questions.length,
          articles: new Set(d.questions.map((q) => q.article_id)).size,
        });
        setQuestions(d.questions.slice(0, PAGE_SIZE));
        return;
      }
      offsetRef.current = offset + d.questions.length;
      // An empty page means the set shrank under us; stop asking for more.
      const total = d.questions.length === 0 ? offset : d.total;
      setTotals({ questions: total, articles: d.articles ?? 0 });
      setQuestions((prev) => [...(prev ?? []), ...d.questions]);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      if (offsetRef.current === 0 && backlogRef.current.length === 0) setErr(message);
      else setMoreErr(message);
    } finally {
      busyRef.current = false;
      setLoadingMore(false);
    }
  }, []);

  useEffect(() => { void loadMore(); }, [loadMore]);

  const shown = questions?.length ?? 0;
  const hasMore = questions !== null && shown < totals.questions;

  // Load the next page as the reader nears the end. Re-created after every
  // page so a sentinel that is still in range triggers again. A failed load
  // stops the auto-loader; the button below retries.
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || !hasMore || moreErr || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(
      (entries) => { if (entries.some((e) => e.isIntersecting)) void loadMore(); },
      { root: el.closest('.main'), rootMargin: '0px 0px 1200px 0px' },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasMore, moreErr, shown, loadMore]);

  const groups = useMemo(() => groupByArticle(questions ?? []), [questions]);

  if (err) {
    return (
      <div className="view">
        <div className="view-h">Today · Open questions</div>
        <p style={{color:'var(--fg-faint)',fontFamily:'var(--mono)',fontSize:12}}>
          couldn't load open questions: {err}
        </p>
      </div>
    );
  }

  if (!questions) {
    return (
      <div className="view">
        <div className="view-h">Today · Open questions</div>
        <p style={{color:'var(--fg-faint)',fontFamily:'var(--mono)',fontSize:12}}>loading…</p>
      </div>
    );
  }

  if (questions.length === 0) {
    return (
      <div className="view">
        <div className="view-h">Today · Open questions</div>
        <h1 className="view-title">No unresolved threads.</h1>
        <p className="view-sub">
          When your agents flag something they can't answer yet, it'll show up here.
        </p>
      </div>
    );
  }

  return (
    <div className="view">
      <div className="view-h">Today · Open questions</div>
      <h1 className="view-title">Unresolved threads.</h1>
      <p className="view-sub">
        {totals.questions.toLocaleString()} open question{totals.questions === 1 ? '' : 's'} across{' '}
        {totals.articles.toLocaleString()} page{totals.articles === 1 ? '' : 's'}.
        Click a page to pick up where the agents left off.
      </p>

      <div style={{display:'flex',flexDirection:'column',gap:18,marginTop:20}}>
        {groups.map((g) => (
          <article
            key={g.id}
            className="thread oq-group"
            onClick={() => onNavigate('article', g.id)}
            style={{cursor:'pointer'}}
          >
            <div className="thread-meta" style={{display:'flex',alignItems:'center',gap:10}}>
              <span className={`art-kind ${g.kind.toLowerCase()}`} style={{fontSize:10}}>
                <span className="k-glyph"/>{g.kind}
              </span>
              <span>{g.items.length} open</span>
            </div>
            <h2 className="thread-title" style={{marginTop:4}}>
              {/* The real link: keyboard focus, Enter, and open-in-new-tab. A plain
                  click falls through to the card's handler above. */}
              <a
                className="oq-link"
                href={routeToPath('article', g.id)}
                onClick={(e) => {
                  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) {
                    e.stopPropagation();
                    return;
                  }
                  e.preventDefault();
                }}
              >
                {g.title}
              </a>
            </h2>
            <div style={{display:'flex',flexDirection:'column',gap:12,marginTop:8}}>
              {g.items.map((q, i) => (
                <div
                  key={`${g.id}-${i}`}
                  className="open"
                  onClick={(e) => {
                    // Let a link inside the body win; otherwise propagate to the article.
                    const t = e.target as HTMLElement;
                    if (t.closest('a')) e.stopPropagation();
                  }}
                >
                  <div style={{display:'flex',alignItems:'center',gap:8,margin:'0 0 4px'}}>
                    <h2 style={{fontSize:14,margin:0,flex:1}}>{q.heading}</h2>
                    <button
                      type="button"
                      style={{
                        fontSize: 11, padding: '2px 8px',
                        border: '1px solid var(--border)', borderRadius: 4,
                        background: 'var(--bg-soft, transparent)', cursor: 'pointer',
                      }}
                      onClick={(e) => {
                        e.stopPropagation();
                        setCaptureCtx({
                          article_id: q.article_id,
                          section_heading: 'Open questions',
                          question_html: q.body,
                        });
                      }}
                    >
                      + add thoughts
                    </button>
                  </div>
                  <SafeHtml
                    as="p"
                    style={{margin:0,fontFamily:'var(--serif)',lineHeight:1.55}}
                    html={q.body}
                  />
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>

      <div ref={sentinelRef} aria-hidden="true"/>
      <div className="oq-more" role="status">
        <span>
          Showing {shown.toLocaleString()} of {totals.questions.toLocaleString()} questions
          {moreErr ? ` — couldn't load more: ${moreErr}` : ''}
        </span>
        {hasMore && (
          <button className="chip" type="button" disabled={loadingMore} onClick={() => void loadMore()}>
            {loadingMore ? 'Loading…' : moreErr ? 'Retry' : 'Load more'}
          </button>
        )}
      </div>

      <NoteCaptureModal
        open={captureCtx !== null}
        onClose={() => setCaptureCtx(null)}
        onCaptured={() => setCaptureCtx(null)}
        context={captureCtx ?? undefined}
      />
    </div>
  );
}
