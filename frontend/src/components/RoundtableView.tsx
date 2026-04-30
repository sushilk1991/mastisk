import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { api, ApiError } from '../api';
import type { Roundtable, View } from '../types';

interface Props {
  roundtableId: number;
  onNavigate: (view: View, id?: string) => void;
}

export function RoundtableView({ roundtableId, onNavigate }: Props) {
  const [rt, setRt] = useState<Roundtable | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const mountedRef = useRef(true);
  const rtRef = useRef<Roundtable | null>(null);

  // Keep a ref in sync with rt so the polling catch can read the latest
  // known status without rebuilding the effect closure on every state change.
  useEffect(() => { rtRef.current = rt; }, [rt]);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Reset view state whenever we switch to a different roundtable. Without
  // this, the old synthesis/title flashes in while the new fetch is in-flight.
  // rtRef is cleared synchronously so the polling effect's catch can't read
  // the previous roundtable's status before the `setRt(null)` settles.
  useEffect(() => {
    setRt(null);
    setErr(null);
    rtRef.current = null;
  }, [roundtableId]);

  // Polling loop while status is pending/running. Re-fetches in the same
  // effect that cancels on unmount and on id changes.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const fetchOnce = async () => {
      try {
        const data = await api.roundtables.get(roundtableId);
        if (cancelled) return;
        setRt(data);
        setErr(null);
        if (data.status === 'pending' || data.status === 'running') {
          timer = window.setTimeout(fetchOnce, 2000);
        }
      } catch (e) {
        if (cancelled) return;
        const apiErr = e instanceof ApiError ? e : null;
        if (apiErr && apiErr.status === 404) {
          setErr('roundtable not found');
          return;  // terminal — don't retry
        }
        // Transient (network, 5xx). Retry with backoff only if the last
        // known status was non-terminal; otherwise surface the error.
        const lastStatus = rtRef.current?.status;
        if (!lastStatus || lastStatus === 'pending' || lastStatus === 'running') {
          timer = window.setTimeout(fetchOnce, 5000);
        } else {
          setErr(e instanceof Error ? e.message : 'failed');
        }
      }
    };

    void fetchOnce();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [roundtableId]);

  const onSaveAsNote = useCallback(async () => {
    if (!rt) return;
    setSaving(true);
    try {
      const res = await api.roundtables.saveAsNote(rt.id);
      if (!mountedRef.current) return;
      onNavigate('note', String(res.note_id));
    } catch (e) {
      if (!mountedRef.current) return;
      setErr(e instanceof Error ? e.message : 'save failed');
      setSaving(false);
    }
  }, [rt, onNavigate]);

  if (err && !rt) return <div className="view"><p style={{ color: 'var(--danger, crimson)' }}>{err}</p></div>;
  if (!rt) return <div className="view"><p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading…</p></div>;

  const inputLink = rt.input_type === 'note' && rt.input_ref
    ? <a href={`/notes/${rt.input_ref}`} onClick={(e) => { e.preventDefault(); onNavigate('note', rt.input_ref); }}>note #{rt.input_ref}</a>
    : rt.input_type === 'article' && rt.input_ref
    ? <a href={`/a/${encodeURIComponent(rt.input_ref)}`} onClick={(e) => { e.preventDefault(); onNavigate('article', rt.input_ref); }}>{rt.input_ref}</a>
    : <span>free-form prompt</span>;

  return (
    <div className="view">
      <div className="view-h">Roundtable #{rt.id}</div>
      <h1 className="view-title">{rt.prompt.length > 80 ? rt.prompt.slice(0, 80) + '…' : rt.prompt}</h1>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)', marginBottom: 12 }}>
        {new Date(rt.created_at).toLocaleString()} · input: {inputLink} · status: <strong>{rt.status}</strong>
        {rt.synthesis_model && <> · synth: {rt.synthesis_model}</>}
      </div>

      {(rt.status === 'pending' || rt.status === 'running') && (
        <p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>
          fanning out to backends — this can take 10-30s…
        </p>
      )}

      {err && (
        <div style={{ color: 'var(--danger, crimson)', marginBottom: 12, fontSize: 12 }}>
          {err}
        </div>
      )}

      {rt.synthesis && (
        <section style={{ marginBottom: 24 }}>
          <h3 style={{ fontSize: 13, color: 'var(--fg-faint)', marginBottom: 8, fontFamily: 'var(--mono)' }}>
            Synthesis · {rt.synthesis_model ?? 'unknown'}
          </h3>
          <div className="md" style={{
            fontSize: 14, lineHeight: 1.5,
            border: '1px solid var(--border)', borderRadius: 6, padding: 12,
            background: 'var(--bg-soft, transparent)',
          }}>
            <ReactMarkdown>{rt.synthesis}</ReactMarkdown>
          </div>
        </section>
      )}

      {rt.perspectives.length > 0 && (
        <section style={{ marginBottom: 24 }}>
          <h3 style={{ fontSize: 13, color: 'var(--fg-faint)', marginBottom: 8, fontFamily: 'var(--mono)' }}>
            Perspectives · {rt.perspectives.length}
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {rt.perspectives.map((p) => (
              <div
                key={p.backend}
                style={{
                  border: '1px solid var(--border)', borderRadius: 6, padding: 10,
                  background: p.error ? 'var(--bg-soft, transparent)' : 'transparent',
                }}
              >
                <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)', marginBottom: 6 }}>
                  <strong style={{ color: 'var(--fg)' }}>{p.backend}</strong>
                  {p.model && <> · {p.model}</>}
                  {p.latency_ms !== null && <> · {(p.latency_ms / 1000).toFixed(1)}s</>}
                  {p.error && <> · <span style={{ color: 'var(--danger, crimson)' }}>error</span></>}
                </div>
                {p.error ? (
                  <div style={{ fontSize: 12, color: 'var(--danger, crimson)' }}>{p.error}</div>
                ) : (
                  <div className="md" style={{ fontSize: 13 }}>
                    <ReactMarkdown>{p.content ?? ''}</ReactMarkdown>
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
        <button onClick={() => onNavigate('roundtables')}>← all roundtables</button>
        {rt.synthesis && !rt.saved_as_note_id && (
          <button onClick={onSaveAsNote} disabled={saving} style={{ marginLeft: 'auto' }}>
            {saving ? 'saving…' : 'save synthesis as note'}
          </button>
        )}
        {rt.saved_as_note_id && (
          <button onClick={() => onNavigate('note', String(rt.saved_as_note_id))} style={{ marginLeft: 'auto' }}>
            open saved note →
          </button>
        )}
      </div>
    </div>
  );
}
