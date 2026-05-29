import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import type { TweetThread, View } from '../types';

interface Props {
  threadId: number;
  onNavigate: (view: View, id?: string) => void;
  onLoaded?: (thread: TweetThread | null) => void;
}

export function TweetThreadView({ threadId, onNavigate, onLoaded }: Props) {
  const [thread, setThread] = useState<TweetThread | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [pollToken, setPollToken] = useState(0);
  const ref = useRef<TweetThread | null>(null);
  const mounted = useRef(true);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { ref.current = thread; }, [thread]);
  useEffect(() => { if (thread) onLoaded?.(thread); }, [thread, onLoaded]);
  useEffect(() => () => onLoaded?.(null), [onLoaded]);

  useEffect(() => {
    setThread(null);
    setErr(null);
    ref.current = null;
  }, [threadId]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const fetchOnce = async () => {
      try {
        const data = await api.tweets.get(threadId);
        if (cancelled) return;
        setThread(data);
        setErr(null);
        if (data.status === 'pending' || data.status === 'running') {
          timer = window.setTimeout(fetchOnce, 2000);
        }
      } catch (e) {
        if (cancelled) return;
        const apiErr = e instanceof ApiError ? e : null;
        if (apiErr?.status === 404) {
          setErr('thread not found');
          return;
        }
        const last = ref.current?.status;
        if (!last || last === 'pending' || last === 'running') {
          timer = window.setTimeout(fetchOnce, 5000);
        } else {
          setErr(e instanceof Error ? e.message : 'failed to refresh thread');
        }
      }
    };
    fetchOnce();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [threadId, pollToken]);

  const copy = useCallback(async () => {
    if (!thread) return;
    const text = thread.thread.map((tweet, i) => `${i + 1}/${thread.thread.length} ${tweet}`).join('\n\n');
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setErr('clipboard unavailable');
    }
  }, [thread]);

  const regenerate = useCallback(async () => {
    if (!thread) return;
    setRegenerating(true);
    setErr(null);
    try {
      await api.tweets.regenerate(thread.id);
      if (!mounted.current) return;
      setThread(prev => prev ? { ...prev, status: 'pending', error: null, finished_at: null, thread: [] } : prev);
      setPollToken(t => t + 1);
    } catch (e) {
      if (!mounted.current) return;
      setErr(e instanceof Error ? e.message : 'regenerate failed');
    } finally {
      if (mounted.current) setRegenerating(false);
    }
  }, [thread]);

  const remove = useCallback(async () => {
    if (!thread) return;
    if (!confirm(`Delete thread #${thread.id}?`)) return;
    setDeleting(true);
    try {
      await api.tweets.delete(thread.id);
      if (!mounted.current) return;
      onNavigate('tweets');
    } catch (e) {
      if (!mounted.current) return;
      setErr(e instanceof Error ? e.message : 'delete failed');
      setDeleting(false);
    }
  }, [thread, onNavigate]);

  if (err && !thread) return <div className="view"><p style={{ color: 'var(--danger, crimson)' }}>{err}</p></div>;
  if (!thread) return <div className="view"><p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading...</p></div>;

  const title = thread.title || (thread.status === 'failed' ? 'Thread failed' : 'Thread in progress');

  return (
    <div className="view">
      <div className="view-h">Tweet Thread · #{thread.id}</div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
        <h1 className="view-title" style={{ margin: 0 }}>{title}</h1>
        <div style={{ display: 'flex', gap: 8 }}>
          {thread.thread.length > 0 && (
            <button onClick={copy}>{copied ? 'copied' : 'Copy thread'}</button>
          )}
          {(thread.status === 'done' || thread.status === 'failed') && (
            <button onClick={regenerate} disabled={regenerating}>{regenerating ? 'regenerating...' : 'Regenerate'}</button>
          )}
          <button onClick={remove} disabled={deleting}>{deleting ? 'deleting...' : 'Delete'}</button>
        </div>
      </div>

      <div style={{ marginTop: 10, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)' }}>
        {new Date(thread.created_at).toLocaleString()} · {thread.window_days}d · {thread.status}
        {thread.model && <> · {thread.model}</>}
        {thread.finished_at && <> · finished {new Date(thread.finished_at).toLocaleString()}</>}
      </div>
      {thread.theme && (
        <div style={{ marginTop: 10, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)' }}>
          theme: {thread.theme}
        </div>
      )}
      {thread.url && (
        <div style={{ marginTop: 6, fontFamily: 'var(--mono)', fontSize: 11 }}>
          <a href={thread.url} target="_blank" rel="noreferrer">{thread.url}</a>
        </div>
      )}
      {thread.angle && <p className="view-sub" style={{ marginTop: 12 }}>{thread.angle}</p>}
      {err && <p style={{ color: 'var(--danger, crimson)' }}>{err}</p>}

      {(thread.status === 'pending' || thread.status === 'running') && (
        <p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>
          Drafting... this usually takes 30-90s.
        </p>
      )}
      {thread.status === 'failed' && thread.error && (
        <p style={{ color: 'var(--danger, crimson)' }}>{thread.error}</p>
      )}

      {thread.thread.length > 0 && (
        <div style={{ display: 'grid', gap: 10, marginTop: 18, maxWidth: 760 }}>
          {thread.thread.map((tweet, i) => (
            <div
              key={`${i}-${tweet.slice(0, 20)}`}
              style={{ border: '1px solid var(--line)', borderRadius: 6, padding: 12 }}
            >
              <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)', marginBottom: 6 }}>
                {i + 1}/{thread.thread.length} · {tweet.length}/280
              </div>
              <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{tweet}</div>
            </div>
          ))}
        </div>
      )}

      {thread.sources.length > 0 && (
        <div style={{ marginTop: 24 }}>
          <div className="view-h">Sources</div>
          <div style={{ display: 'grid', gap: 8, maxWidth: 760 }}>
            {thread.sources.map((source, i) => (
              <div key={i} style={{ borderTop: '1px solid var(--line)', paddingTop: 8 }}>
                <div style={{ fontSize: 13, fontWeight: 500 }}>{source.title || source.url || source.kind || 'source'}</div>
                {source.why && <div style={{ fontSize: 12, color: 'var(--fg-mute)', marginTop: 3 }}>{source.why}</div>}
                {source.url && (
                  <a href={source.url} target="_blank" rel="noreferrer" style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>
                    {source.url}
                  </a>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {thread.warnings.length > 0 && (
        <div style={{ marginTop: 24 }}>
          <div className="view-h">Warnings</div>
          <ul>
            {thread.warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}
