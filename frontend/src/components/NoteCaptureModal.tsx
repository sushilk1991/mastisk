import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { useModalA11y } from '../hooks/useModalA11y';
import type { TranscriptAnchor } from '../types';

export interface CaptureContext {
  article_id: string;
  section_heading?: string;
  question_html?: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  onCaptured?: (noteId: number) => void;
  context?: CaptureContext;
  /**
   * Optional anchor pinning this capture to a transcript segment of a podcast/youtube
   * source. Persisted as notes.transcript_anchor_json so the PodcastView can render
   * the note inline under its segment on subsequent loads.
   */
  transcriptAnchor?: TranscriptAnchor | null;
}

function stripHtml(html: string): string {
  const div = document.createElement('div');
  div.innerHTML = html;
  return div.textContent?.trim() ?? '';
}

export function NoteCaptureModal({ open, onClose, onCaptured, context, transcriptAnchor }: Props) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);

  const { modalRef, ariaProps } = useModalA11y({
    open,
    onClose,
    initialFocusRef: ref,
  });

  useEffect(() => {
    if (open) {
      setText('');
      setError(null);
    }
  }, [open]);

  const submit = useCallback(async () => {
    const trimmed = text.trim();
    if (!trimmed) { setError('empty note'); return; }
    setBusy(true);
    setError(null);
    try {
      const res = await api.notes.create(trimmed, context, transcriptAnchor ?? null);
      onCaptured?.(res.id);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'failed');
    } finally {
      setBusy(false);
    }
  }, [text, onCaptured, onClose, context, transcriptAnchor]);

  // ⌘↵ to submit. Escape is handled by the a11y hook globally.
  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      void submit();
    }
  }, [submit]);

  if (!open) return null;

  const questionPreview = context?.question_html ? stripHtml(context.question_html) : '';

  return (
    <div
      className="note-capture-backdrop"
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.3)',
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        paddingTop: '10vh', zIndex: 1000,
      }}
      onClick={onClose}
    >
      <div
        ref={modalRef}
        {...ariaProps}
        aria-labelledby="note-capture-title"
        tabIndex={-1}
        className="note-capture-card"
        style={{
          background: 'var(--bg)', border: '1px solid var(--border)',
          borderRadius: 8, padding: 16, width: 'min(640px, 92vw)',
          boxShadow: '0 8px 32px rgba(0,0,0,0.15)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {context && (
          <div
            style={{
              fontSize: 12, color: 'var(--fg-faint)', marginBottom: 10,
              paddingBottom: 8, borderBottom: '1px solid var(--border)',
            }}
          >
            in reply to {context.section_heading ? `"${context.section_heading}"` : 'article'}:
            <div style={{ fontStyle: 'italic', marginTop: 4 }}>
              {questionPreview ? questionPreview : <em>(article context)</em>}
            </div>
          </div>
        )}
        <div
          id="note-capture-title"
          style={{ fontSize: 12, color: 'var(--fg-faint)', marginBottom: 8, fontFamily: 'var(--mono)' }}
        >
          capture note — ⌘↵ to save, esc to cancel
        </div>
        <textarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
          placeholder={context ? 'your thought on this question' : 'what are you thinking?'}
          rows={8}
          style={{
            width: '100%', boxSizing: 'border-box',
            background: 'transparent', color: 'var(--fg)',
            border: '1px solid var(--border)', borderRadius: 4,
            padding: 10, fontFamily: 'var(--mono)', fontSize: 14,
            resize: 'vertical',
          }}
        />
        {error && <div style={{ color: 'var(--danger, crimson)', marginTop: 6, fontSize: 12 }}>{error}</div>}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 10 }}>
          <button onClick={onClose} disabled={busy}>cancel</button>
          <button onClick={submit} disabled={busy || !text.trim()}>
            {busy ? 'saving…' : 'save ⌘↵'}
          </button>
        </div>
      </div>
    </div>
  );
}
