import { useEffect, useState } from 'react';
import { api } from '../api';
import type { OpenQuestion, View } from '../types';
import { NoteCaptureModal, type CaptureContext } from './NoteCaptureModal';
import { SafeHtml } from './SafeHtml';

interface Props {
  onNavigate: (view: View, id?: string) => void;
}

export function OpenQuestionsView({ onNavigate }: Props) {
  const [questions, setQuestions] = useState<OpenQuestion[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [captureCtx, setCaptureCtx] = useState<CaptureContext | null>(null);

  useEffect(() => {
    api.openQuestions()
      .then((d) => setQuestions(d.questions))
      .catch((e: Error) => setErr(e.message));
  }, []);

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

  // Group by article so a reader sees all the threads under one page.
  const groups: { id: string; title: string; kind: string; updated_at: string; items: OpenQuestion[] }[] = [];
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

  return (
    <div className="view">
      <div className="view-h">Today · Open questions</div>
      <h1 className="view-title">Unresolved threads.</h1>
      <p className="view-sub">
        {questions.length} open question{questions.length === 1 ? '' : 's'} across {groups.length} page{groups.length === 1 ? '' : 's'}.
        Click a page to pick up where the agents left off.
      </p>

      <div style={{display:'flex',flexDirection:'column',gap:18,marginTop:20}}>
        {groups.map((g) => (
          <article
            key={g.id}
            className="thread"
            onClick={() => onNavigate('article', g.id)}
            style={{cursor:'pointer'}}
            role="link"
          >
            <div className="thread-meta" style={{display:'flex',alignItems:'center',gap:10}}>
              <span className={`art-kind ${g.kind.toLowerCase()}`} style={{fontSize:10}}>
                <span className="k-glyph"/>{g.kind}
              </span>
              <span>{g.items.length} open</span>
            </div>
            <h2 className="thread-title" style={{marginTop:4}}>{g.title}</h2>
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

      <NoteCaptureModal
        open={captureCtx !== null}
        onClose={() => setCaptureCtx(null)}
        onCaptured={() => setCaptureCtx(null)}
        context={captureCtx ?? undefined}
      />
    </div>
  );
}
