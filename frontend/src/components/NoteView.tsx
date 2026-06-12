import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { Note, View } from '../types';
import { MarkdownBlock, VaultMarkdownEditor, type VaultEditorTarget } from './MarkdownEditor';

interface Props {
  noteId: number;
  onNavigate: (view: View, id?: string) => void;
}

export function NoteView({ noteId, onNavigate }: Props) {
  const [note, setNote] = useState<Note | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [escalating, setEscalating] = useState(false);
  const [editorTarget, setEditorTarget] = useState<VaultEditorTarget | null>(null);

  const loadNote = useCallback(async () => {
    setNote(null);
    setErr(null);
    try {
      setNote(await api.notes.get(noteId));
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed');
    }
  }, [noteId]);

  useEffect(() => { void loadNote(); }, [loadNote]);

  const onDelete = useCallback(async () => {
    if (!note) return;
    if (!confirm(`Delete note?\n\n${note.summary ?? note.slug}`)) return;
    setDeleting(true);
    try {
      await api.notes.delete(note.id);
      onNavigate('notes');
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'delete failed');
      setDeleting(false);
    }
  }, [note, onNavigate]);

  const onEscalate = useCallback(async () => {
    if (!note) return;
    setEscalating(true);
    try {
      await api.notes.escalate(note.id);
      const updated = await api.notes.get(note.id);
      setNote(updated);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'escalate failed');
    } finally {
      setEscalating(false);
    }
  }, [note]);

  if (err) return <div className="view"><p style={{ color: 'var(--danger, crimson)' }}>{err}</p></div>;
  if (!note) return <div className="view"><p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading…</p></div>;

  return (
    <div className="view">
      <div className="view-h">Note · {note.source}</div>
      <h1 className="view-title">{note.summary ?? note.slug}</h1>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)', marginBottom: 12 }}>
        {new Date(note.created_at).toLocaleString()}
        {note.classification && <> · {note.classification}</>}
        {note.tags.length > 0 && <> · {note.tags.map(t => `#${t}`).join(' ')}</>}
      </div>
      {(note.escalation_state === 'auto_done' || note.escalation_state === 'manual_done') && note.escalation_article_id && (
        <div style={{ fontSize: 12, color: 'var(--fg-faint)', marginBottom: 8 }}>
          researched →{' '}
          <a
            href={`/a/${encodeURIComponent(note.escalation_article_id)}`}
            onClick={(e) => { e.preventDefault(); onNavigate('article', note.escalation_article_id!); }}
            style={{ color: 'var(--fg)' }}
          >
            {note.escalation_article_id}
          </a>
        </div>
      )}
      {note.related_articles && note.related_articles.length > 0 && (
        <section style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 11, color: 'var(--fg-faint)', fontFamily: 'var(--mono)', marginBottom: 6 }}>
            Related · {note.related_articles.length}
          </div>
          <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {note.related_articles.map(r => (
              <li key={r.article_id}>
                <a
                  href={`/a/${encodeURIComponent(r.article_id)}`}
                  onClick={(e) => { e.preventDefault(); onNavigate('article', r.article_id); }}
                  style={{
                    fontSize: 12, color: 'var(--fg)',
                    textDecoration: 'none',
                    padding: '2px 8px',
                    border: '1px solid var(--border)', borderRadius: 4,
                    background: 'var(--bg-soft, transparent)',
                  }}
                >
                  {r.title}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
      <div className="markdown-preview note-markdown-body">
        <MarkdownBlock source={note.body ?? ''}/>
      </div>
      <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
        <button onClick={() => onNavigate('notes')}>← all notes</button>
        <button onClick={() => setEditorTarget({ path: note.path, title: note.summary ?? note.slug })}>
          edit
        </button>
        <button
          onClick={async () => {
            try {
              const res = await api.roundtables.create(
                'note',
                String(note.id),
                note.summary ?? note.body?.slice(0, 100) ?? 'discuss',
              );
              onNavigate('roundtable', String(res.id));
            } catch (e) {
              setErr(e instanceof Error ? e.message : 'roundtable failed');
            }
          }}
          title="Fan this note out to Claude + Codex + Gemini + Ollama and synthesize."
        >
          roundtable
        </button>
        <button
          disabled={deleting || escalating || note.escalation_state === 'pending' || note.escalation_state === 'retrying'}
          onClick={onEscalate}
          title={
            note.escalation_state === 'auto_done' || note.escalation_state === 'manual_done'
              ? `Already researched (article: ${note.escalation_article_id ?? 'unknown'}). Click to re-research.`
              : 'Research this via Claude — creates a wiki article stub linked back to this note.'
          }
        >
          {escalating ? 'sending…'
            : note.escalation_state === 'pending' || note.escalation_state === 'retrying' ? 'researching…'
            : note.escalation_state === 'auto_done' || note.escalation_state === 'manual_done' ? 're-research'
            : 'research this'}
        </button>
        <button onClick={onDelete} disabled={deleting} style={{ marginLeft: 'auto' }}>
          {deleting ? 'deleting…' : 'delete'}
        </button>
      </div>
      {editorTarget && (
        <VaultMarkdownEditor
          target={editorTarget}
          onClose={() => setEditorTarget(null)}
          onSaved={loadNote}
        />
      )}
    </div>
  );
}
