import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { useModalA11y } from '../hooks/useModalA11y';

interface Props {
  open: boolean;
  /** Optional pre-fill for the theme textarea. When the modal opens the field
   *  resets to this value (or empty when omitted) — match-on-create relies on
   *  the user not editing it, so this is how "Draft this →" pipes a topic
   *  suggestion's title through to the backend's auto-link. */
  initialTheme?: string;
  onClose: () => void;
  onCreated: (id: number) => void;
}

const WINDOW_CHOICES = [7, 14, 30, 90] as const;
type WindowChoice = (typeof WINDOW_CHOICES)[number];

export function BlogCreationModal({ open, initialTheme, onClose, onCreated }: Props) {
  const [theme, setTheme] = useState('');
  const [windowDays, setWindowDays] = useState<WindowChoice>(14);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const { modalRef, ariaProps } = useModalA11y({
    open,
    onClose,
    initialFocusRef: textareaRef,
    canClose: () => !busy,
  });

  useEffect(() => {
    if (open) {
      setTheme(initialTheme ?? '');
      setWindowDays(14);
      setBusy(false);
      setError(null);
    }
  }, [open, initialTheme]);

  // Abort any in-flight submit when the modal unmounts. Also cleared on
  // next submit (see below) so only the latest call is live.
  useEffect(() => () => abortRef.current?.abort(), []);

  const submit = useCallback(async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setBusy(true);
    setError(null);
    try {
      const res = await api.blog.create(
        { theme: theme.trim(), window_days: windowDays },
        ctrl.signal,
      );
      if (ctrl.signal.aborted) return;
      onCreated(res.id);
    } catch (e) {
      if ((e as Error).name === 'AbortError') return;
      // 422 from the backend for empty source pool carries a specific message;
      // rewrite it to a user-friendly version instead of showing raw API copy.
      const status = (e as { status?: number }).status;
      const raw = e instanceof Error ? e.message : 'failed';
      if (status === 422 && raw.toLowerCase().includes('no sources')) {
        setError('No recent synthesis to draw from. Try capturing some notes or running a roundtable first.');
      } else {
        setError(raw);
      }
      setBusy(false);
    }
  }, [theme, windowDays, onCreated]);

  // ⌘↵ to submit. Escape is handled globally by the a11y hook, gated on !busy.
  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      void submit();
    }
  }, [submit]);

  if (!open) return null;

  return (
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.3)',
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        paddingTop: '10vh', zIndex: 1000,
      }}
      onClick={busy ? undefined : onClose}
    >
      <div
        ref={modalRef}
        {...ariaProps}
        aria-labelledby="blog-creation-title"
        tabIndex={-1}
        style={{
          background: 'var(--bg)', border: '1px solid var(--line)',
          borderRadius: 8, padding: 16, width: 'min(560px, 92vw)',
          boxShadow: 'var(--shadow-md, 0 8px 32px rgba(0,0,0,0.15))',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          id="blog-creation-title"
          style={{ fontSize: 12, color: 'var(--fg-faint)', marginBottom: 8, fontFamily: 'var(--mono)' }}
        >
          draft a blog post — ⌘↵ to submit, esc to cancel
        </div>

        <div style={{ fontSize: 11, color: 'var(--fg-faint)', marginBottom: 4, fontFamily: 'var(--mono)' }}>
          Theme (optional)
        </div>
        <textarea
          ref={textareaRef}
          value={theme}
          onChange={(e) => setTheme(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
          placeholder="Optional — a theme or question to anchor the post around"
          rows={4}
          maxLength={500}
          style={{
            width: '100%', boxSizing: 'border-box',
            background: 'transparent', color: 'var(--fg)',
            border: '1px solid var(--line)', borderRadius: 4,
            padding: 10, fontFamily: 'var(--mono)', fontSize: 14,
            resize: 'vertical',
          }}
        />
        <div style={{ fontSize: 10, color: 'var(--fg-faint)', fontFamily: 'var(--mono)', textAlign: 'right' }}>
          {theme.length}/500
        </div>

        <div style={{ fontSize: 11, color: 'var(--fg-faint)', marginTop: 12, marginBottom: 6, fontFamily: 'var(--mono)' }}>
          Source window
        </div>
        <div
          style={{
            display: 'flex', gap: 0, marginBottom: 4,
            border: '1px solid var(--line)', borderRadius: 4, overflow: 'hidden',
          }}
          role="radiogroup"
          aria-label="Source window in days"
        >
          {WINDOW_CHOICES.map((n) => {
            const active = n === windowDays;
            return (
              <button
                key={n}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => setWindowDays(n)}
                disabled={busy}
                style={{
                  flex: 1,
                  padding: '6px 10px',
                  border: 'none',
                  background: active ? 'var(--fg)' : 'transparent',
                  color: active ? 'var(--bg)' : 'var(--fg)',
                  cursor: busy ? 'default' : 'pointer',
                  fontSize: 12,
                  fontFamily: 'var(--mono)',
                }}
              >
                {n} days
              </button>
            );
          })}
        </div>
        <div style={{ fontSize: 11, color: 'var(--fg-faint)', fontFamily: 'var(--mono)' }}>
          Pulls notes, articles, and roundtable syntheses from the last {windowDays} days.
        </div>

        {error && (
          <div style={{ color: 'var(--danger, crimson)', marginTop: 10, fontSize: 12 }}>
            {error}
          </div>
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
          <button onClick={onClose} disabled={busy}>cancel</button>
          <button onClick={submit} disabled={busy}>
            {busy ? 'drafting…' : 'draft ⌘↵'}
          </button>
        </div>
      </div>
    </div>
  );
}
