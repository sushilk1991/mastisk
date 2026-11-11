import { useEffect, useState } from 'react';
import { api } from '../api';
import type { DigestAudit, DigestAuditCandidate, View } from '../types';
import '../styles/views/DigestAuditView.css';

interface Props {
  date?: string;
  onNavigate: (view: View, id?: string) => void;
}

const WINDOWS = [
  { days: 1, label: '1 day' },
  { days: 7, label: '7 days' },
  { days: 30, label: '30 days' },
];

export function DigestAuditView({ date, onNavigate }: Props) {
  const [days, setDays] = useState(7);
  const [data, setData] = useState<DigestAudit | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    setError(null);
    setBusy(true);
    try {
      const r = await api.digestAudit(date, days);
      setData(r);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { void load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [date, days]);

  if (error) {
    return (
      <div className="view">
        <div className="view-h">Digest audit</div>
        <p className="view-sub">Error: {error}</p>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="view">
        <div className="view-h">Digest audit</div>
        <p className="view-sub">{busy ? 'loading…' : 'idle'}</p>
      </div>
    );
  }

  const rollup = data.rollup;
  const hitPct = rollup.total_shown ? Math.round(rollup.hit_rate * 100) : 0;

  return (
    <div className="view">
      <div className="view-h" style={{display:'flex',alignItems:'center',gap:12}}>
        <button
          type="button"
          onClick={() => onNavigate('digest', date)}
          style={{background:'none',border:'none',cursor:'pointer',color:'var(--fg-mute)',fontFamily:'var(--mono)',fontSize:11}}
          title="Back to digest"
        >
          ← digest
        </button>
        <span>Digest audit · {data.start} → {data.end}</span>
      </div>
      <h1 className="view-title">Why those articles, over the last {data.window_days} day{data.window_days === 1 ? '' : 's'}.</h1>
      <p className="view-sub">
        {rollup.total_shown} of {rollup.total_considered} candidates were shown. You liked {rollup.liked},
        disliked {rollup.disliked}, and didn't rate {rollup.unrated}. Hit rate {hitPct}%.
      </p>

      <div className="audit-controls">
        <span style={{color:'var(--fg-mute)',fontFamily:'var(--mono)',fontSize:11}}>window</span>
        {WINDOWS.map((w) => (
          <button
            key={w.days}
            type="button"
            className={`audit-range${days === w.days ? ' is-on' : ''}`}
            onClick={() => setDays(w.days)}
          >
            {w.label}
          </button>
        ))}
      </div>

      {data.days.length === 0 && (
        <p className="view-sub" style={{marginTop: 24}}>
          No candidates logged in this window yet. Open the digest page to trigger scoring for today.
        </p>
      )}

      {data.days.map((day) => (
        <AuditDay
          key={day.date}
          date={day.date}
          candidates={day.candidates}
          onOpenArticle={(id) => onNavigate('article', id)}
        />
      ))}
    </div>
  );
}

function AuditDay({
  date, candidates, onOpenArticle,
}: {
  date: string;
  candidates: DigestAuditCandidate[];
  onOpenArticle: (id: string) => void;
}) {
  const shown = candidates.filter((c) => c.selected);
  const hidden = candidates.filter((c) => !c.selected);
  return (
    <div className="audit-day">
      <div className="audit-day-h">
        <span className="audit-day-date">{date}</span>
        <span className="audit-day-sub">
          {shown.length} shown · {hidden.length} considered · not shown
        </span>
      </div>

      <div className="audit-section-h">selected</div>
      {shown.length === 0 && (
        <div className="audit-empty">(no articles met the bar on this date)</div>
      )}
      {shown.map((c) => (
        <AuditRow key={c.article_id} cand={c} onOpenArticle={onOpenArticle} />
      ))}

      {hidden.length > 0 && (
        <>
          <div className="audit-section-h">also considered</div>
          {hidden.slice(0, 10).map((c) => (
            <AuditRow key={c.article_id} cand={c} onOpenArticle={onOpenArticle} muted />
          ))}
          {hidden.length > 10 && (
            <div className="audit-empty">
              + {hidden.length - 10} more below final_score {(hidden[10]?.final ?? 0).toFixed(2)}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function AuditRow({
  cand, onOpenArticle, muted,
}: {
  cand: DigestAuditCandidate;
  onOpenArticle: (id: string) => void;
  muted?: boolean;
}) {
  const fbColor =
    cand.feedback === 'yes' ? 'var(--kind-concept)' :
    cand.feedback === 'no'  ? 'var(--kind-source)'  : 'var(--fg-faint)';
  const fbLabel =
    cand.feedback === 'yes' ? '👍 liked' :
    cand.feedback === 'no'  ? '👎 disliked' : '— unrated';

  return (
    <div className={`audit-row${muted ? ' is-muted' : ''}`}>
      <div className="audit-row-rank">
        {cand.rank ? `#${cand.rank}` : '—'}
      </div>
      <div className="audit-row-main">
        <button
          type="button"
          className="audit-row-title"
          onClick={() => onOpenArticle(cand.article_id)}
          title={cand.summary}
        >
          {cand.title}
        </button>
        <div className="audit-row-meta">
          <span className="audit-kind">{cand.kind}</span>
          <span title="intrinsic quality">q {cand.quality.toFixed(2)}</span>
          <span title="personal relevance">i {cand.interest.toFixed(2)}</span>
          <span title="final score">= {cand.final.toFixed(2)}</span>
          <span style={{color: fbColor}}>{fbLabel}</span>
        </div>
      </div>
    </div>
  );
}
