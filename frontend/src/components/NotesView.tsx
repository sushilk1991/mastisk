import { useEffect, useState } from 'react';
import { api } from '../api';
import type { Note, View } from '../types';

interface Props {
  onNavigate: (view: View, id?: string) => void;
  onCaptureNote: () => void;
}

export function NotesView({ onNavigate, onCaptureNote }: Props) {
  const [notes, setNotes] = useState<Note[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.notes.list(100)
      .then(setNotes)
      .catch((e) => setErr(e instanceof Error ? e.message : 'failed'));
  }, []);

  if (err) return <div className="view"><p className="dash-error">{err}</p></div>;
  if (!notes) {
    return (
      <div className="view notes-view">
        <div className="dash-skeleton" aria-label="Loading notes">
          <span/>
          <span/>
          <span/>
        </div>
      </div>
    );
  }

  if (notes.length === 0) {
    return (
      <div className="view notes-view">
        <header className="view-head">
          <div className="view-head-copy">
            <div className="view-h">Personal OS</div>
            <h1 className="view-title">Notes</h1>
            <p className="view-sub">Raw captures before Mastisk classifies, links, or promotes them.</p>
          </div>
          <div className="view-head-actions">
            <button className="new-action" type="button" onClick={onCaptureNote}>+ New</button>
          </div>
        </header>
        <div className="dash-empty-state">
          <p>No notes yet. Capture the first thought from here.</p>
          <button className="chip" type="button" onClick={onCaptureNote}>+ New note</button>
        </div>
      </div>
    );
  }

  return (
    <div className="view notes-view">
      <header className="view-head">
        <div className="view-head-copy">
          <div className="view-h">Personal OS</div>
          <h1 className="view-title">Notes</h1>
          <p className="view-sub">{notes.length} {notes.length === 1 ? 'raw capture' : 'raw captures'} waiting in the note stream.</p>
        </div>
        <div className="view-head-actions">
          <button className="new-action" type="button" onClick={onCaptureNote}>+ New</button>
        </div>
      </header>
      <div className="notes-list">
        {notes.map((n) => (
          <button
            key={n.id}
            onClick={() => onNavigate('note', String(n.id))}
            className="notes-row"
            title={n.summary ?? n.slug}
          >
            <div className="notes-meta">
              {new Date(n.created_at).toLocaleString()} · {n.source}
              {n.classification && <> · <span>{n.classification}</span></>}
              {!n.classification && <> · <span>unclassified</span></>}
              {n.escalation_state !== 'none' && (
                <span className="notes-state">[{n.escalation_state}]</span>
              )}
            </div>
            <div className="notes-title">
              {n.summary ?? n.slug}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
