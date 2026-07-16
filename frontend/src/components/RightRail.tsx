import { useEffect, useState } from 'react';
import type { Article, AgentInfo, FeedTick, Note, View } from '../types';
import { ArtifactPanel } from './ArtifactPanel';
import { api } from '../api';
import { Icon } from './icons';

interface Props {
  article: Article;
  feed: FeedTick[];
  agents: AgentInfo[];
  onAsk: (prompt: string, selection: string | null) => void;
  onNavigate: (view: View, id?: string) => void;
  onClose: () => void;
}

export function RightRail({ article, feed, agents, onAsk, onNavigate, onClose }: Props) {
  return (
    <aside className="rail">
      <div className="rail-mobile-head">
        <strong>Page context</strong>
        <button type="button" onClick={onClose} aria-label="Close page context">{Icon.close}</button>
      </div>
      <button
        type="button"
        className="rail-ask"
        onClick={() => onAsk(`Help me think through “${article.title}”`, null)}
      >
        <span>{Icon.ask}</span>
        <span><strong>Ask about this page</strong><small>Chat with your whole wiki</small></span>
      </button>
      <div className="rail-section">
        <div className="rail-h">
          Concept map
          {article.related.length > 0 && (
            <span className="count">{Math.min(article.related.length, 6)}/{article.related.length}</span>
          )}
        </div>
        <MiniGraph article={article} onNavigate={onNavigate}/>
      </div>

      <div className="rail-section">
        <div className="rail-h">Related <span className="count">{article.related.length}</span></div>
        {article.related.map((r) => (
          <div key={r.id} className="rel-row" onClick={() => onNavigate('article', r.id)}>
            <span className="rel-label">{r.label}</span>
            <span className="rel-bar"><span className="fill" style={{ width: `${r.weight*100}%` }}/></span>
            <span className="rel-w">{r.weight.toFixed(2)}</span>
          </div>
        ))}
      </div>

      <YourThoughts articleId={article.id} onNavigate={onNavigate}/>

      <ArtifactPanel article={article}/>

      <div className="rail-section">
        <div className="rail-h">Backlinks <span className="count">{article.backlinks}</span></div>
        {!article.backlinkList || article.backlinkList.length === 0 ? (
          <div style={{ fontSize: 12, color: 'var(--fg-faint)', lineHeight: 1.5 }}>
            No inbound links yet. When other pages reference this one, they'll appear here.
          </div>
        ) : (
          article.backlinkList.map((b) => (
            <div
              key={b.id}
              className="backlink"
              onClick={() => onNavigate('article', b.id)}
              style={{ cursor: 'pointer' }}
              title={b.title}
            >
              <div className="bl-title">{b.title}</div>
              {b.snippet && <div className="bl-snip">{b.snippet}</div>}
            </div>
          ))
        )}
      </div>

      <div className="rail-section">
        <div className="rail-h">Live feed
          <span style={{display:'inline-flex',alignItems:'center',gap:5,fontSize:9,color:'var(--accent)'}}>
            <span style={{width:5,height:5,borderRadius:'50%',background:'var(--accent)',animation:'pulse 1.6s infinite'}}/>
            LIVE
          </span>
        </div>
        {feed.slice(0, 6).map((f, i) => (
          <div key={i} className="tick-row">
            <div className="tick-time">{f.t}</div>
            <div className="tick-body">
              <span className={`tick-agent ${agents.find((a) => a.id === f.agent)?.color || ''}`}>{f.agent}</span>
              <span className="tick-verb"> {f.verb}</span>{' '}
              <span className="tick-obj">{f.obj}</span>
              {f.touched > 0 && <div style={{fontSize:10,color:'var(--fg-faint)',fontFamily:'var(--mono)',marginTop:2}}>touched {f.touched} pages</div>}
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}

function YourThoughts({
  articleId,
  onNavigate,
}: {
  articleId: string;
  onNavigate: (v: View, id?: string) => void;
}) {
  const [notes, setNotes] = useState<Note[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    setNotes(null);
    api.notes.forArticle(articleId)
      .then((rows) => { if (!cancelled) setNotes(rows); })
      .catch(() => { if (!cancelled) setNotes([]); });
    return () => { cancelled = true; };
  }, [articleId]);

  return (
    <div className="rail-section">
      <div className="rail-h">
        Your thoughts
        <span className="count">{notes?.length ?? 0}</span>
      </div>
      {notes === null ? (
        <div style={{ fontSize: 12, color: 'var(--fg-faint)', fontFamily: 'var(--mono)' }}>loading…</div>
      ) : notes.length === 0 ? (
        <div style={{ fontSize: 12, color: 'var(--fg-faint)', lineHeight: 1.5 }}>
          No thoughts yet. Look at the Open Questions section and hit "+ add thoughts" to start.
        </div>
      ) : (
        <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {notes.map((n) => (
            <li key={n.id}>
              <a
                href={`/notes/${n.id}`}
                style={{ fontSize: 12, color: 'var(--fg)', textDecoration: 'none', display: 'block' }}
                onClick={(e) => { e.preventDefault(); onNavigate('note', String(n.id)); }}
              >
                <span style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', marginRight: 6 }}>
                  {new Date(n.created_at).toLocaleDateString()}
                </span>
                {n.summary ?? n.slug}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const KIND_GLYPH: Record<string, string> = {
  Concept: '▲',
  Entity: '●',
  Source: '◊',
  Synthesis: '✦',
};

function shortLabel(label: string, max = 14): string {
  const clean = label.replace(/[\u2014\u2013]/g, '-').trim();
  if (clean.length <= max) return clean;
  // Prefer cutting at a word boundary
  const cut = clean.slice(0, max);
  const space = cut.lastIndexOf(' ');
  return (space > 6 ? cut.slice(0, space) : cut) + '…';
}

function MiniGraph({ article, onNavigate }: { article: Article; onNavigate: (v: View, id?: string) => void }) {
  const rels = article.related.slice(0, 6);
  // Empty state — a degenerate graph is worse than honest silence.
  if (rels.length === 0) {
    return (
      <div className="mini-graph mini-graph--empty">
        <div className="mini-empty">No links yet. Agents are still reading.</div>
      </div>
    );
  }

  // Elliptical orbit. Semi-axes tuned so 6 pills fit without overflowing
  // the container on the horizontal axis or clashing with the centre pill
  // on the vertical axis.
  const W = 100, H = 100;            // SVG viewBox in %-like units
  const cx = W / 2, cy = H / 2;
  const rx = 32, ry = 34;             // orbit semi-axes (% of box)
  const glyph = KIND_GLYPH[article.kind] ?? '•';
  const centerLabel = shortLabel(article.title, 16);

  const nodes = rels.map((r, i) => {
    const angle = (i / rels.length) * Math.PI * 2 - Math.PI / 2;
    return {
      ...r,
      x: cx + Math.cos(angle) * rx,
      y: cy + Math.sin(angle) * ry,
      label: shortLabel(r.label, 14),
    };
  });

  return (
    <div className="mini-graph">
      {/* Edges: SVG so thickness + opacity both scale smoothly. */}
      <svg className="mini-edges" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
        {nodes.map((n) => (
          <line
            key={`e-${n.id}`}
            x1={cx} y1={cy} x2={n.x} y2={n.y}
            className="mini-edge"
            style={{
              strokeWidth: 0.4 + n.weight * 0.8,
              strokeOpacity: 0.2 + n.weight * 0.55,
            }}
          />
        ))}
      </svg>

      {/* Related nodes */}
      {nodes.map((n) => (
        <button
          key={n.id}
          type="button"
          className="mini-pill"
          style={{
            left: `${n.x}%`,
            top: `${n.y}%`,
            // Weak links sit slightly ghosted; strong ones are fully present.
            opacity: 0.55 + n.weight * 0.45,
          }}
          onClick={() => onNavigate('article', n.id)}
          title={`${article.related.find((r) => r.id === n.id)?.label ?? n.id}  ·  ${n.weight.toFixed(2)}`}
        >
          {n.label}
        </button>
      ))}

      {/* Center: the article itself */}
      <div className="mini-center" title={article.title}>
        <span className={`mini-center-glyph k-${article.kind.toLowerCase()}`}>{glyph}</span>
        <span className="mini-center-label">{centerLabel}</span>
      </div>
    </div>
  );
}
