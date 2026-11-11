import { useEffect, useState } from 'react';
import { api } from '../api';
import { localDateTime } from '../date';
import type { RoundtableSummary, View } from '../types';

interface Props {
  onNavigate: (view: View, id?: string) => void;
}

export function RoundtablesListView({ onNavigate }: Props) {
  const [rows, setRows] = useState<RoundtableSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.roundtables.list(100)
      .then(setRows)
      .catch((e) => setErr(e instanceof Error ? e.message : 'failed'));
  }, []);

  if (err) return <div className="view"><p style={{ color: 'var(--danger, crimson)' }}>{err}</p></div>;
  if (!rows) return <div className="view"><p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading…</p></div>;
  if (rows.length === 0) {
    return (
      <div className="view">
        <div className="view-h">Roundtables</div>
        <h1 className="view-title">No roundtables yet</h1>
        <p className="view-sub">
          Open any note and hit "roundtable" to fan a prompt out to Claude + Codex + Gemini + Ollama and get a synthesis.
        </p>
      </div>
    );
  }

  return (
    <div className="view">
      <div className="view-h">Roundtables</div>
      <h1 className="view-title">{rows.length} roundtable{rows.length === 1 ? '' : 's'}</h1>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
        {rows.map((r) => (
          <button
            key={r.id}
            onClick={() => onNavigate('roundtable', String(r.id))}
            style={{
              textAlign: 'left', padding: 10,
              border: '1px solid var(--border)', borderRadius: 6,
              background: 'var(--bg-soft, transparent)',
              cursor: 'pointer',
            }}
          >
            <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)' }}>
              #{r.id} · {localDateTime(r.created_at)} · {r.input_type}
              {' · '}
              <span style={{
                color: r.status === 'failed' ? 'var(--danger, crimson)'
                     : r.status === 'done' ? 'var(--fg)'
                     : 'var(--fg-faint)',
              }}>
                {r.status}
              </span>
              {r.synthesis_model && <> · synth: {r.synthesis_model}</>}
            </div>
            <div style={{ fontSize: 13, marginTop: 4 }}>
              {r.synthesis_preview ?? <em style={{ color: 'var(--fg-faint)' }}>(no synthesis yet)</em>}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
