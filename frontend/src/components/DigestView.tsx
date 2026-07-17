import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { api } from '../api';
import type { Digest, DigestThread, View } from '../types';

interface Props {
  digest: Digest;
  onNavigate: (view: View, id?: string) => void;
  onAsk: (prompt: string, selection: string | null) => void;
}

export function TodayDigestSection({ digest, onNavigate, onAsk }: Props) {
  const empty = digest.counters.every((counter) => counter.value === 0) && digest.threads.length === 0;
  return (
    <section className="dash-section today-digest" aria-labelledby="today-digest-title">
      <div className="dash-section-head today-digest-head">
        <div>
          <span className="today-digest-kicker">Your agents’ reading, in one place</span>
          <h2 id="today-digest-title">Daily digest</h2>
        </div>
        <button className="chip" type="button" onClick={() => onNavigate('digest_audit', digest.iso_date)}>
          Why these?
        </button>
      </div>
      <p className="today-digest-summary">{digest.summary}</p>
      {empty ? (
        <div className="today-digest-empty">
          No new reading made today’s cut. The rest of Today still works normally.
        </div>
      ) : (
        <>
          <div className="today-digest-counters" aria-label="Daily digest summary">
            {digest.counters.filter((counter) => counter.value > 0).map((counter) => (
              <span key={counter.label}><b>{counter.value}</b> {counter.label}</span>
            ))}
          </div>
          <div className="today-digest-threads">
            {digest.threads.map((thread, index) => (
              <ThreadCard
                key={thread.article_id ?? index}
                index={index}
                thread={thread}
                digestDate={digest.iso_date}
                onNavigate={onNavigate}
                onAsk={onAsk}
              />
            ))}
          </div>
        </>
      )}
    </section>
  );
}

export function DigestView({ digest, onNavigate, onAsk }: Props) {
  const empty = digest.counters.every((c) => c.value === 0);
  const hasNav = digest.prev_date || digest.next_date;

  if (empty && digest.threads.length === 0 && !hasNav) {
    return <EmptyDigest onAsk={onAsk} />;
  }

  if (empty && digest.threads.length === 0) {
    return (
      <div className="view">
        <DigestNav digest={digest} onNavigate={onNavigate}/>
        <h1 className="view-title">No agent activity on {digest.iso_date}.</h1>
        <p className="view-sub">{digest.summary}</p>
      </div>
    );
  }

  return (
    <div className="view">
      <DigestNav digest={digest} onNavigate={onNavigate}/>
      <h1 className="view-title">What your agents read while you slept.</h1>
      <p className="view-sub">{digest.summary}</p>

      <div className="counters">
        {digest.counters.map((c, i) => (
          <div key={i} className="counter">
            <div className="v">{c.value}</div>
            <div className="l">{c.label}</div>
          </div>
        ))}
      </div>

      <div
        style={{
          display: 'flex', alignItems: 'baseline', justifyContent: 'space-between',
          marginBottom: 4, gap: 16,
        }}
      >
        <div style={{fontFamily:'var(--mono)',fontSize:10,textTransform:'uppercase',letterSpacing:'0.08em',color:'var(--fg-faint)'}}>Threads</div>
        <button
          type="button"
          className="digest-audit-link"
          onClick={() => onNavigate('digest_audit', digest.iso_date)}
          title="See what was considered and why"
        >
          audit ranker →
        </button>
      </div>

      {digest.threads.map((t, i) => (
        <ThreadCard
          key={t.article_id ?? i}
          index={i}
          thread={t}
          digestDate={digest.iso_date}
          onNavigate={onNavigate}
          onAsk={onAsk}
        />
      ))}

      <div className="queue-block">
        <div style={{fontFamily:'var(--mono)',fontSize:10,textTransform:'uppercase',letterSpacing:'0.08em',color:'var(--fg-faint)',marginBottom:10}}>In progress · agents are working</div>
        {digest.queue.map((q, i) => (
          <div key={i} className={`queue-row ${i===1 ? 'now' : ''}`}>
            <div className="q-status"/>
            <span>{q}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ThreadCard({
  index, thread, digestDate, onNavigate, onAsk,
}: {
  index: number;
  thread: DigestThread;
  digestDate: string;
  onNavigate: Props['onNavigate'];
  onAsk: Props['onAsk'];
}) {
  const [verdict, setVerdict] = useState<'yes' | 'no' | null>(thread.feedback ?? null);
  const [busy, setBusy] = useState(false);

  async function vote(next: 'yes' | 'no') {
    if (busy || !thread.article_id) return;
    setBusy(true);
    const optimistic = verdict === next ? 'clear' : next;
    const prior = verdict;
    setVerdict(optimistic === 'clear' ? null : next);
    try {
      await api.digestFeedback(thread.article_id, optimistic, digestDate);
    } catch {
      setVerdict(prior);
    } finally {
      setBusy(false);
    }
  }

  const scores = thread.scores;
  return (
    <div className="thread">
      <div className="thread-meta">
        <span>thread #{index + 1}</span>
        <span>{thread.sources} sources</span>
        {scores && (
          <span className="thread-scores" title={`quality ${scores.quality} · interest ${scores.interest} · final ${scores.final}`}>
            q {scores.quality.toFixed(2)} · i {scores.interest.toFixed(2)}
          </span>
        )}
        <span className="thread-feedback">
          <button
            type="button"
            className={`thumb${verdict === 'yes' ? ' is-on' : ''}`}
            onClick={() => vote('yes')}
            disabled={busy || !thread.article_id}
            aria-pressed={verdict === 'yes'}
            title="Show me more like this"
          >
            👍
          </button>
          <button
            type="button"
            className={`thumb${verdict === 'no' ? ' is-on' : ''}`}
            onClick={() => vote('no')}
            disabled={busy || !thread.article_id}
            aria-pressed={verdict === 'no'}
            title="Less like this"
          >
            👎
          </button>
        </span>
      </div>
      <h2
        className="thread-title"
        title={thread.title}
        onClick={() => thread.article_id && onNavigate('article', thread.article_id)}
      >
        {thread.title}
      </h2>
      <div className="thread-body">
        <ReactMarkdown>{digestBodyMarkdown(thread.body)}</ReactMarkdown>
      </div>
      <div className="thread-links">
        {thread.links.map((l) => (
          <span key={l} className="chip" onClick={() => onAsk(`Tell me about ${l}`, l)}>{l}</span>
        ))}
      </div>
    </div>
  );
}

function digestBodyMarkdown(body: string): string {
  return body
    .replace(/<\/?em>/gi, '*')
    .replace(/<[^>]+>/g, '');
}

function DigestNav({ digest, onNavigate }: { digest: Digest; onNavigate: Props['onNavigate'] }) {
  const go = (d: string | null) => { if (d) onNavigate('digest', d); };
  const btn = (disabled: boolean): React.CSSProperties => ({
    background: 'none',
    border: 'none',
    cursor: disabled ? 'default' : 'pointer',
    color: disabled ? 'var(--fg-faint)' : 'var(--fg-mute)',
    fontFamily: 'var(--mono)',
    fontSize: 11,
    padding: '0 6px',
    opacity: disabled ? 0.4 : 1,
  });
  return (
    <div
      className="view-h"
      style={{display:'flex',alignItems:'center',gap:6,flexWrap:'wrap'}}
    >
      <button
        type="button"
        aria-label="Previous day"
        style={btn(!digest.prev_date)}
        disabled={!digest.prev_date}
        onClick={() => go(digest.prev_date)}
        title={digest.prev_date ?? 'No earlier activity'}
      >
        ←{digest.prev_date ? ` ${digest.prev_date}` : ''}
      </button>
      <span>{digest.date} · Daily Digest</span>
      <button
        type="button"
        aria-label="Next day"
        style={btn(!digest.next_date)}
        disabled={!digest.next_date}
        onClick={() => go(digest.next_date)}
        title={digest.next_date ?? 'No later activity'}
      >
        {digest.next_date ? `${digest.next_date} ` : ''}→
      </button>
    </div>
  );
}

function EmptyDigest({ onAsk }: { onAsk: Props['onAsk'] }) {
  return (
    <div className="view">
      <div className="view-h">Mastisk · Empty wiki</div>
      <h1 className="view-title">Nothing in your wiki yet.</h1>
      <p className="view-sub">
        Your agents are ready. Subscribe a feed, queue a video, or just ask a question —
        the wiki fills itself from there.
      </p>

      <div className="queue-block" style={{marginTop: 32}}>
        <div style={{fontFamily:'var(--mono)',fontSize:10,textTransform:'uppercase',letterSpacing:'0.08em',color:'var(--fg-faint)',marginBottom:14}}>
          Start here — from your Mac terminal
        </div>
        <div style={{fontFamily:'var(--mono)',fontSize:13,lineHeight:1.8,color:'var(--fg)'}}>
          <div><span style={{color:'var(--fg-faint)'}}>$</span> mastisk add-feed <span style={{color:'var(--accent)'}}>https://simonwillison.net/atom/everything/</span></div>
          <div><span style={{color:'var(--fg-faint)'}}>$</span> mastisk add-youtube <span style={{color:'var(--accent)'}}>https://youtube.com/watch?v=…</span></div>
          <div><span style={{color:'var(--fg-faint)'}}>$</span> mastisk seed-demo <span style={{color:'var(--fg-faint)'}}>  # load the Test-time compute sample</span></div>
        </div>
      </div>

      <div className="queue-block" style={{marginTop: 16}}>
        <div style={{fontFamily:'var(--mono)',fontSize:10,textTransform:'uppercase',letterSpacing:'0.08em',color:'var(--fg-faint)',marginBottom:10}}>
          Or just ask
        </div>
        <div style={{display:'flex', flexWrap:'wrap', gap:6}}>
          <button className="chip" onClick={() => onAsk('What should I read first?', null)}>
            What should I read first?
          </button>
          <button className="chip" onClick={() => onAsk('Summarize my interests file', null)}>
            Summarize my interests file
          </button>
          <button className="chip" onClick={() => onAsk('What is agent memory?', null)}>
            What is agent memory?
          </button>
        </div>
      </div>

      <div style={{marginTop:32, fontFamily:'var(--serif)', fontStyle:'italic', color:'var(--fg-mute)', fontSize:15}}>
        Edit <code style={{fontFamily:'var(--mono)',fontSize:12,background:'var(--bg-sunk)',padding:'1px 6px',borderRadius:4}}>vault/_self/interests.md</code> to shape what Scout pays attention to.
        It lives in iCloud — editable from your phone too.
      </div>
    </div>
  );
}
