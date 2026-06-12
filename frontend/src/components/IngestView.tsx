import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { Feed } from '../types';

interface Toast { text: string; tone: 'ok' | 'err' | 'info' }

interface ListenResult { jobId: number; kind: string; message: string }
type JobStatus = 'queued' | 'running' | 'done' | 'failed';

export function IngestView() {
  const [feeds, setFeeds] = useState<Feed[] | null>(null);
  const [url, setUrl] = useState('');
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);

  const [listenUrl, setListenUrl] = useState('');
  const [listenBusy, setListenBusy] = useState(false);
  const [listenOk, setListenOk] = useState<ListenResult | null>(null);
  const [listenErr, setListenErr] = useState<string | null>(null);

  const [docFile, setDocFile] = useState<File | null>(null);
  const [docBusy, setDocBusy] = useState(false);
  const [docJobId, setDocJobId] = useState<number | null>(null);
  const [docStatus, setDocStatus] = useState<JobStatus | null>(null);
  const [docErr, setDocErr] = useState<string | null>(null);
  const docInputRef = useRef<HTMLInputElement | null>(null);

  const reload = async () => {
    try {
      const d = await api.feeds();
      setFeeds(d.feeds);
    } catch (e) {
      setToast({ text: String(e), tone: 'err' });
    }
  };

  useEffect(() => { void reload(); }, []);

  useEffect(() => {
    if (!docJobId || docStatus === 'done' || docStatus === 'failed') return;
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await api.ingest.job(docJobId);
        if (!cancelled) {
          setDocStatus(res.job.status);
          if (res.job.status === 'failed') {
            setDocErr(res.job.error || 'document ingest failed');
          }
        }
      } catch (err) {
        if (!cancelled) setDocErr(err instanceof Error ? err.message : String(err));
      }
    };
    const timer = window.setInterval(() => { void poll(); }, 1800);
    void poll();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [docJobId, docStatus]);

  const flash = (t: Toast) => {
    setToast(t);
    setTimeout(() => setToast(null), 2400);
  };

  const onAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    const u = url.trim();
    if (!u) return;
    setBusy(true);
    try {
      await api.addFeed(u, title.trim() || undefined);
      setUrl(''); setTitle('');
      await reload();
      flash({ text: 'subscribed', tone: 'ok' });
    } catch (err) {
      flash({ text: String(err), tone: 'err' });
    } finally {
      setBusy(false);
    }
  };

  const onRemove = async (u: string) => {
    if (!confirm(`Unsubscribe from\n${u}?`)) return;
    await api.removeFeed(u);
    await reload();
    flash({ text: 'removed', tone: 'info' });
  };

  const onFetchNow = async (u: string) => {
    await api.fetchFeedNow(u);
    flash({ text: 'fetching now — watch the ticker', tone: 'ok' });
    setTimeout(() => reload(), 4000);
  };

  const onListen = async (e: React.FormEvent) => {
    e.preventDefault();
    const u = listenUrl.trim();
    if (!u) return;
    setListenBusy(true);
    setListenErr(null);
    setListenOk(null);
    try {
      const res = await api.listen(u);
      setListenUrl('');
      setListenOk({ jobId: res.job_id, kind: res.kind, message: res.message });
    } catch (err) {
      setListenErr(err instanceof Error ? err.message : String(err));
    } finally {
      setListenBusy(false);
    }
  };

  const onDocumentUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!docFile) return;
    setDocBusy(true);
    setDocErr(null);
    setDocStatus(null);
    try {
      const res = await api.ingest.uploadDocument(docFile);
      setDocJobId(res.job_id);
      setDocStatus('queued');
      setDocFile(null);
      if (docInputRef.current) {
        docInputRef.current.value = '';
      }
    } catch (err) {
      setDocErr(err instanceof Error ? err.message : String(err));
    } finally {
      setDocBusy(false);
    }
  };

  return (
    <div className="view">
      <div className="view-h">System · Sources & ingest</div>
      <h1 className="view-title">Feeds your agents watch.</h1>
      <p className="view-sub">
        Scout polls each enabled feed every 10 minutes and sends matching items to Compiler.
        Add one here, or via <code style={{fontFamily:'var(--mono)',fontSize:12,background:'var(--bg-sunk)',padding:'1px 6px',borderRadius:4}}>mastisk add-feed &lt;url&gt;</code>.
      </p>

      <div className="view-h" style={{marginTop:24}}>Document upload</div>
      <form onSubmit={onDocumentUpload} className="listen-row" style={{margin:'12px 0 28px'}}>
        <input
          ref={docInputRef}
          type="file"
          accept=".pdf,.docx,.pptx,.xlsx,.html,.txt,.md,.epub"
          onChange={(e) => {
            setDocFile(e.target.files?.[0] ?? null);
            setDocErr(null);
          }}
          className="listen-input"
        />
        <button
          type="submit"
          disabled={docBusy || !docFile}
          style={btnPrimary(docBusy || !docFile)}
        >
          {docBusy ? 'queuing…' : 'Upload'}
        </button>
      </form>
      {docJobId && (
        <div className="listen-ok">
          <span className="listen-ok-id">#{docJobId}</span>
          <span className="listen-ok-copy">document ingest <em>{docStatus || 'queued'}</em></span>
        </div>
      )}
      {docErr && <div className="listen-err">{docErr}</div>}

      <form onSubmit={onAdd} style={{display:'flex',gap:8,margin:'24px 0 32px',flexWrap:'wrap'}}>
        <input
          type="url"
          required
          placeholder="https://simonwillison.net/atom/everything/"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          style={inputStyle(true)}
        />
        <input
          type="text"
          placeholder="title (optional)"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          style={inputStyle(false)}
        />
        <button type="submit" disabled={busy || !url.trim()} style={btnPrimary(busy || !url.trim())}>
          {busy ? 'adding…' : 'Subscribe'}
        </button>
      </form>

      <div className="view-h" style={{marginTop:16}}>Subscribed feeds {feeds && `· ${feeds.length}`}</div>

      {!feeds && <p style={{color:'var(--fg-faint)',fontFamily:'var(--mono)',fontSize:12}}>loading…</p>}

      {feeds && feeds.length === 0 && (
        <div style={{padding:'24px',border:'1px dashed var(--line)',borderRadius:8,fontFamily:'var(--serif)',color:'var(--fg-mute)'}}>
          No feeds yet. Paste an RSS/Atom URL above to subscribe.
          <div style={{marginTop:10,fontSize:13}}>
            Try: Simon Willison, Karpathy, Dwarkesh, Latent Space, Lilian Weng, Every, Lenny's Newsletter.
          </div>
        </div>
      )}

      {feeds && feeds.length > 0 && (
        <div style={{border:'1px solid var(--line)',borderRadius:8,overflow:'hidden'}}>
          {feeds.map((f) => (
            <FeedRow key={f.url} feed={f} onRemove={onRemove} onFetchNow={onFetchNow}/>
          ))}
        </div>
      )}

      <div className="view-h" style={{marginTop:48}}>Paste a link</div>
      <p className="listen-hint">
        YouTube, podcast RSS feeds, or direct audio URLs. Spotify episodes aren't supported (DRM).
      </p>

      <form onSubmit={onListen} className="listen-row">
        <input
          type="url"
          required
          placeholder="https://www.youtube.com/watch?v=…"
          value={listenUrl}
          onChange={(e) => { setListenUrl(e.target.value); if (listenErr) setListenErr(null); }}
          className="listen-input"
        />
        <button
          type="submit"
          disabled={listenBusy || !listenUrl.trim()}
          style={btnPrimary(listenBusy || !listenUrl.trim())}
        >
          {listenBusy ? 'queuing…' : 'Queue'}
        </button>
      </form>

      {listenOk && (
        <div className="listen-ok">
          <span className="listen-ok-id">#{listenOk.jobId}</span>
          <span className="listen-ok-copy">
            queued as <em>{listenOk.kind}</em>. {listenOk.message || 'Listener will pick it up shortly.'}
          </span>
        </div>
      )}

      {listenErr && (
        <div className="listen-err">{listenErr}</div>
      )}

      {toast && (
        <div className="toast" style={{
          background: toast.tone === 'err' ? '#c53030' : toast.tone === 'ok' ? 'var(--fg)' : 'var(--bg-elev)',
          color: toast.tone === 'info' ? 'var(--fg)' : 'var(--fg-inv)',
          border: toast.tone === 'info' ? '1px solid var(--line)' : 'none',
        }}>{toast.text}</div>
      )}
    </div>
  );
}

function FeedRow({ feed, onRemove, onFetchNow }: {
  feed: Feed;
  onRemove: (url: string) => void;
  onFetchNow: (url: string) => void;
}) {
  const last = feed.last_fetched ? timeAgo(feed.last_fetched) : 'never';
  return (
    <div style={{display:'grid',gridTemplateColumns:'1fr auto auto',gap:12,padding:'14px 16px',borderTop:'1px solid var(--line-soft)',alignItems:'center'}}>
      <div style={{overflow:'hidden'}}>
        <div style={{fontSize:14,color:'var(--fg)',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{feed.title || feed.url}</div>
        <div style={{fontFamily:'var(--mono)',fontSize:11,color:'var(--fg-faint)',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
          <a href={feed.url} target="_blank" rel="noreferrer" style={{color:'inherit'}}>{feed.url}</a>
        </div>
        <div style={{fontFamily:'var(--mono)',fontSize:10,color:'var(--fg-faint)',marginTop:3}}>
          last synced: <span style={{color: feed.last_fetched ? 'var(--fg-mute)' : 'var(--accent)'}}>{last}</span>
          {feed.last_etag && <span style={{marginLeft:10}}>etag cached</span>}
          {!feed.enabled && <span style={{marginLeft:10,color:'var(--accent)'}}>disabled</span>}
        </div>
      </div>
      <button onClick={() => onFetchNow(feed.url)} style={btnGhost()} title="Fetch this feed right now">
        Fetch now
      </button>
      <button onClick={() => onRemove(feed.url)} style={btnGhost('danger')} title="Unsubscribe">
        Remove
      </button>
    </div>
  );
}

function timeAgo(iso: string): string {
  const delta = Date.now() - new Date(iso.replace(' ', 'T') + 'Z').getTime();
  if (delta < 0) return 'just now';
  const s = Math.floor(delta / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

function inputStyle(grow: boolean): React.CSSProperties {
  return {
    flex: grow ? 1 : '0 0 auto',
    minWidth: grow ? 260 : 160,
    padding: '10px 12px',
    borderRadius: 6,
    background: 'var(--bg-card)',
    color: 'var(--fg)',
    border: '1px solid var(--line)',
    fontSize: 13,
    fontFamily: 'var(--sans)',
  };
}

function btnPrimary(disabled: boolean): React.CSSProperties {
  return {
    padding: '10px 18px',
    borderRadius: 6,
    background: disabled ? 'var(--bg-sunk)' : 'var(--accent)',
    color: disabled ? 'var(--fg-faint)' : 'var(--fg-inv)',
    fontSize: 13,
    fontWeight: 500,
    cursor: disabled ? 'not-allowed' : 'pointer',
  };
}

function btnGhost(tone: 'normal' | 'danger' = 'normal'): React.CSSProperties {
  return {
    padding: '7px 12px',
    borderRadius: 5,
    background: 'transparent',
    color: tone === 'danger' ? 'var(--fg-mute)' : 'var(--fg)',
    fontSize: 12,
    border: '1px solid var(--line)',
    cursor: 'pointer',
  };
}
