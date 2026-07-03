import { useEffect, useRef, useState, type CSSProperties, type DragEvent, type FormEvent } from 'react';
import { api } from '../api';
import type { Feed } from '../types';

interface Toast { text: string; tone: 'ok' | 'err' | 'info' }

interface ListenResult { jobId: number; kind: string; message: string }
type JobStatus = 'queued' | 'running' | 'done' | 'failed';
type ImportMode = 'text' | 'url' | 'document';

type ImportResult =
  | { kind: 'text'; noteId: number; path: string }
  | { kind: 'url'; jobId: number; message: string }
  | {
      kind: 'document';
      jobId: number;
      status: JobStatus;
      sourcePath?: string | null;
      noteId?: number | null;
      notePath?: string | null;
      sourceId?: string | null;
      compilerJobId?: number | null;
      compilerJobStatus?: JobStatus | null;
      compilerJobCreated?: boolean | null;
      provider?: string | null;
    };

const DOCUMENT_ACCEPT = '.pdf,.docx,.pptx,.xlsx,.html,.txt,.md,.epub';

export function IngestView() {
  const [feeds, setFeeds] = useState<Feed[] | null>(null);
  const [feedUrl, setFeedUrl] = useState('');
  const [feedTitle, setFeedTitle] = useState('');
  const [feedBusy, setFeedBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);

  const [importText, setImportText] = useState('');
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [importErr, setImportErr] = useState<string | null>(null);
  const [docJobId, setDocJobId] = useState<number | null>(null);
  const [docStatus, setDocStatus] = useState<JobStatus | null>(null);
  const docInputRef = useRef<HTMLInputElement | null>(null);
  const mode = routeMode(importFile, importText);

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
          const parsed = parseDocumentResult(res.job.result);
          setImportResult((current) => current?.kind === 'document' && current.jobId === docJobId
            ? { ...current, status: res.job.status, ...parsed }
            : current);
          if (res.job.status === 'failed') {
            setImportErr(res.job.error || 'document ingest failed');
          }
        }
      } catch (err) {
        if (!cancelled) setImportErr(err instanceof Error ? err.message : String(err));
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

  const onAdd = async (e: FormEvent) => {
    e.preventDefault();
    const u = feedUrl.trim();
    if (!u) return;
    setFeedBusy(true);
    try {
      await api.addFeed(u, feedTitle.trim() || undefined);
      setFeedUrl(''); setFeedTitle('');
      await reload();
      flash({ text: 'subscribed', tone: 'ok' });
    } catch (err) {
      flash({ text: String(err), tone: 'err' });
    } finally {
      setFeedBusy(false);
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

  const onImport = async (e: FormEvent) => {
    e.preventDefault();
    setImportBusy(true);
    setImportErr(null);
    setImportResult(null);
    try {
      if (importFile) {
        const res = await api.ingest.uploadDocument(importFile);
        const status = normalizeJobStatus(res.status);
        setDocJobId(res.job_id);
        setDocStatus(status);
        setImportResult({
          kind: 'document',
          jobId: res.job_id,
          status,
          sourcePath: res.source_path,
          ...parseDocumentResult(res.result),
        });
        setImportFile(null);
        if (docInputRef.current) docInputRef.current.value = '';
        return;
      }

      const trimmed = importText.trim();
      const singleUrl = parseSingleUrl(trimmed);
      if (singleUrl) {
        const res = await api.listen(singleUrl);
        const queued: ListenResult = { jobId: res.job_id, kind: res.kind, message: res.message };
        setImportText('');
        setImportResult({ kind: 'url', jobId: queued.jobId, message: queued.message });
        return;
      }

      if (!trimmed) return;
      const note = await api.notes.create(trimmed);
      await api.notes.escalate(note.id);
      setImportText('');
      setImportResult({ kind: 'text', noteId: note.id, path: note.path });
    } catch (err) {
      setImportErr(err instanceof Error ? err.message : String(err));
    } finally {
      setImportBusy(false);
    }
  };

  const onFileChange = (file: File | null) => {
    setImportFile(file);
    setImportErr(null);
    setImportResult(null);
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    const file = e.dataTransfer.files?.[0] ?? null;
    if (file) onFileChange(file);
  };

  const disabled = importBusy || (!importFile && !importText.trim());

  return (
    <div className="view ingest-view">
      <div className="view-h">System · Import</div>
      <h1 className="view-title">Import anything into Mastisk.</h1>

      <section className="import-console" aria-label="Import into Mastisk">
        <div className="import-console-head">
          <div>
            <div className="import-eyebrow">New import</div>
            <h2>Paste text, paste a link, or choose a document.</h2>
          </div>
          <span className={`import-route route-${mode}`}>{routeLabel(mode)}</span>
        </div>

        <form onSubmit={onImport} className="import-form">
          <textarea
            value={importText}
            onChange={(e) => {
              setImportText(e.target.value);
              setImportErr(null);
              setImportResult(null);
            }}
            disabled={Boolean(importFile)}
            placeholder="https://example.com/post or paste a tweet thread, excerpt, transcript, article draft..."
            aria-label="Text or URL to import"
          />

          <div
            className={`import-file-drop ${importFile ? 'has-file' : ''}`}
            onDragOver={(e) => e.preventDefault()}
            onDrop={onDrop}
          >
            <input
              ref={docInputRef}
              type="file"
              accept={DOCUMENT_ACCEPT}
              onChange={(e) => onFileChange(e.target.files?.[0] ?? null)}
              className="import-file-input"
              aria-label="Document to import"
            />
            <button
              className="import-file-button"
              type="button"
              onClick={() => docInputRef.current?.click()}
            >
              Choose document
            </button>
            <span className="import-file-name">
              {importFile ? fileLabel(importFile) : 'PDF, DOCX, EPUB, HTML, TXT, MD, PPTX, XLSX'}
            </span>
            {importFile && (
              <button
                className="import-file-clear"
                type="button"
                onClick={() => {
                  onFileChange(null);
                  if (docInputRef.current) docInputRef.current.value = '';
                }}
              >
                clear
              </button>
            )}
          </div>

          <div className="import-actions">
            <div className="import-route-list" aria-label="Import routing">
              <span className={mode === 'text' ? 'active' : ''}>{'text -> note + research'}</span>
              <span className={mode === 'url' ? 'active' : ''}>{'link -> source article'}</span>
              <span className={mode === 'document' ? 'active' : ''}>{'file -> source article'}</span>
            </div>
            <button className="import-primary" type="submit" disabled={disabled}>
              {importBusy ? 'Importing' : 'Import'}
            </button>
          </div>
        </form>

        {importResult && <ImportStatus result={importResult}/>}
        {importErr && <div className="listen-err">{importErr}</div>}
      </section>

      <section className="import-section">
        <div className="import-section-head">
          <div>
            <div className="view-h">Recurring feeds {feeds && `· ${feeds.length}`}</div>
            <h2>Feeds your agents watch.</h2>
          </div>
          <span>Scout polls enabled feeds and sends matching items to Compiler.</span>
        </div>

        <form onSubmit={onAdd} className="feed-subscribe-row">
          <input
            type="url"
            required
            placeholder="https://simonwillison.net/atom/everything/"
            value={feedUrl}
            onChange={(e) => setFeedUrl(e.target.value)}
            className="listen-input"
          />
          <input
            type="text"
            placeholder="title"
            value={feedTitle}
            onChange={(e) => setFeedTitle(e.target.value)}
            className="listen-input feed-title-input"
          />
          <button type="submit" disabled={feedBusy || !feedUrl.trim()} className="import-secondary">
            {feedBusy ? 'Adding' : 'Subscribe'}
          </button>
        </form>

        {!feeds && <p className="import-loading">loading...</p>}

        {feeds && feeds.length === 0 && (
          <div className="import-empty">
            No feeds yet. Add one above or import a single link in the console.
          </div>
        )}

        {feeds && feeds.length > 0 && (
          <div className="feed-list">
            {feeds.map((f) => (
              <FeedRow key={f.url} feed={f} onRemove={onRemove} onFetchNow={onFetchNow}/>
            ))}
          </div>
        )}
      </section>

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

function ImportStatus({ result }: { result: ImportResult }) {
  if (result.kind === 'text') {
    return (
      <div className="listen-ok">
        <span className="listen-ok-id">#{result.noteId}</span>
        <span className="listen-ok-copy">
          note saved and research queued. <em>{result.path}</em>
        </span>
      </div>
    );
  }
  if (result.kind === 'url') {
    return (
      <div className="listen-ok">
        <span className="listen-ok-id">#{result.jobId}</span>
        <span className="listen-ok-copy">{result.message || 'link queued for Listener'}</span>
      </div>
    );
  }
  const done = result.status === 'done';
  const compilerText = done ? documentCompilerText(result) : null;
  return (
    <div className="listen-ok">
      <span className="listen-ok-id">#{result.jobId}</span>
      <span className="listen-ok-copy">
        document ingest <em>{result.status}</em>
        {compilerText ? `; ${compilerText}` : ''}
        {done && result.sourceId ? `; source ${result.sourceId}` : ''}
      </span>
    </div>
  );
}

function documentCompilerText(result: Extract<ImportResult, { kind: 'document' }>): string | null {
  if (!result.compilerJobId) return null;
  if (!result.compilerJobStatus) return `article compile job #${result.compilerJobId}`;
  if (result.compilerJobStatus === 'done') return `article compile done #${result.compilerJobId}`;
  if (result.compilerJobStatus === 'running') return `article compile running #${result.compilerJobId}`;
  if (result.compilerJobStatus === 'failed') return `article compile failed #${result.compilerJobId}`;
  return result.compilerJobCreated === false
    ? `article compile already queued #${result.compilerJobId}`
    : `article compile queued #${result.compilerJobId}`;
}

function routeMode(file: File | null, text: string): ImportMode {
  if (file) return 'document';
  return parseSingleUrl(text.trim()) ? 'url' : 'text';
}

function routeLabel(mode: ImportMode): string {
  if (mode === 'document') return 'document';
  if (mode === 'url') return 'link';
  return 'text';
}

function parseSingleUrl(value: string): string | null {
  if (!value || /\s/.test(value)) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null;
  } catch {
    return null;
  }
}

function parseDocumentResult(result: unknown): Partial<Extract<ImportResult, { kind: 'document' }>> {
  if (!result || typeof result !== 'object') return {};
  const record = result as Record<string, unknown>;
  return {
    noteId: typeof record.note_id === 'number' ? record.note_id : null,
    notePath: typeof record.note_path === 'string' ? record.note_path : null,
    sourcePath: typeof record.source_path === 'string' ? record.source_path : null,
    sourceId: typeof record.source_id === 'string' ? record.source_id : null,
    compilerJobId: typeof record.compiler_job_id === 'number' ? record.compiler_job_id : null,
    compilerJobStatus: typeof record.compiler_job_status === 'string'
      ? normalizeJobStatus(record.compiler_job_status)
      : null,
    compilerJobCreated: typeof record.compiler_job_created === 'boolean'
      ? record.compiler_job_created
      : null,
    provider: typeof record.provider === 'string' ? record.provider : null,
  };
}

function normalizeJobStatus(status: string): JobStatus {
  return status === 'running' || status === 'done' || status === 'failed' ? status : 'queued';
}

function fileLabel(file: File): string {
  const mb = file.size / (1024 * 1024);
  const size = mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(file.size / 1024))} KB`;
  return `${file.name} · ${size}`;
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

function btnGhost(tone: 'normal' | 'danger' = 'normal'): CSSProperties {
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
