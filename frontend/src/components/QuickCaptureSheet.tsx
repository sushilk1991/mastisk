import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { useModalA11y } from '../hooks/useModalA11y';
import type { QuickCaptureResponse, View } from '../types';

interface Props {
  open: boolean;
  onClose: () => void;
  onNavigate: (view: View, id?: string) => void;
  initialHint?: 'note';
}

export function QuickCaptureSheet({ open, onClose, onNavigate, initialHint }: Props) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<QuickCaptureResponse | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { modalRef, ariaProps } = useModalA11y({
    open,
    onClose,
    initialFocusRef: textareaRef,
    canClose: () => !busy,
  });

  useEffect(() => {
    if (!open) return;
    setText('');
    setBusy(false);
    setError(null);
    setResult(null);
  }, [open]);

  const submit = useCallback(async () => {
    const trimmed = text.trim();
    if (!trimmed || busy) {
      if (!trimmed) setError('Please write something to capture.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const captured = initialHint === 'note'
        ? noteCaptureResult(await api.notes.create(trimmed))
        : await api.quickCapture(trimmed, new Date().toISOString());
      setResult(captured);
      setText('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Capture failed.');
    } finally {
      setBusy(false);
    }
  }, [busy, initialHint, text]);

  if (!open) return null;

  const target = result ? destinationTarget(result) : null;
  const placeholder = initialHint === 'note'
    ? 'Write a note. Enter saves; Shift+Enter adds a line.'
    : 'Task, journal line, person update, quote, inventory, idea...';

  return (
    <div
      className="quick-capture-backdrop"
      onClick={() => { if (!busy) onClose(); }}
    >
      <div
        ref={modalRef}
        {...ariaProps}
        aria-labelledby="quick-capture-title"
        tabIndex={-1}
        className="quick-capture-sheet"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="quick-capture-kicker">Quick capture</div>
        <h2 id="quick-capture-title">File anything into Mastisk.</h2>
        <p>Enter saves. Shift+Enter adds a line. Esc closes.</p>

        {!result ? (
          <>
            <textarea
              ref={textareaRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (
                  e.key === 'Enter'
                  && !e.shiftKey
                  && !(e.nativeEvent as KeyboardEvent).isComposing
                ) {
                  e.preventDefault();
                  void submit();
                }
              }}
              disabled={busy}
              placeholder={placeholder}
              aria-label="Quick capture text"
              rows={6}
            />
            {error && <div className="quick-capture-error">{error}</div>}
            <div className="quick-capture-actions">
              <button type="button" className="chip muted" onClick={onClose} disabled={busy}>
                Dismiss
              </button>
              <button
                type="button"
                className="quick-capture-primary"
                onClick={() => void submit()}
                disabled={busy || !text.trim()}
              >
                {busy ? 'Filing...' : 'Capture'}
              </button>
            </div>
          </>
        ) : (
          <div className="quick-capture-confirm" role="status">
            <div className="quick-capture-check" aria-hidden>+</div>
            <div>
              <div className="quick-capture-filed">
                filed as {labelForType(result.type)} -&gt; <span title={result.destination}>{result.destination}</span>
              </div>
              {result.needs_triage && (
                <p className="quick-capture-triage">Marked for triage because confidence was low.</p>
              )}
              <div className="quick-capture-actions">
                {target && (
                  <button
                    type="button"
                    className="quick-capture-primary"
                    onClick={() => {
                      onClose();
                      onNavigate(target.view, target.id);
                    }}
                  >
                    {target.label}
                  </button>
                )}
                <button type="button" className="chip muted" onClick={onClose}>
                  Dismiss
                </button>
                <button
                  type="button"
                  className="chip"
                  onClick={() => {
                    setResult(null);
                    window.requestAnimationFrame(() => textareaRef.current?.focus());
                  }}
                >
                  Capture another
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function noteCaptureResult(note: { id: number; path: string }): QuickCaptureResponse {
  return {
    id: note.id,
    type: 'note',
    destination: note.path,
    needs_triage: false,
  };
}

function labelForType(type: string): string {
  if (type === 'project_update') return 'project update';
  if (type === 'routine_done') return 'routine';
  return type.replace(/_/g, ' ');
}

function destinationTarget(result: QuickCaptureResponse): { label: string; view: View; id?: string } | null {
  switch (result.type) {
    case 'task':
      return { label: 'Open tasks', view: 'tasks' };
    case 'journal':
      return { label: 'Open journal', view: 'journal' };
    case 'project_update':
      return { label: 'Open projects', view: 'projects' };
    case 'routine_done':
      return { label: 'Open routines', view: 'routines' };
    case 'person':
      return { label: 'Open person', view: 'people' };
    case 'quote':
      return { label: 'Open quote', view: 'library', id: `quote:${String(result.id)}` };
    case 'inventory':
      return { label: 'Open inventory', view: 'inventory' };
    case 'content':
      return { label: 'Open content', view: 'content' };
    case 'note':
    case 'inbox':
      return { label: 'Open note', view: 'note', id: String(result.id) };
    default:
      return null;
  }
}
