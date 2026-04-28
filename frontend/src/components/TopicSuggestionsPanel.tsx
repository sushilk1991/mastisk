import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { TopicSuggestion, TopicSuggestionKind, TopicSourceRef } from '../types';

interface Props {
  /** Called when the user clicks "Draft this →". Receives the suggestion's
   *  title verbatim — the parent should pass it to BlogCreationModal as the
   *  initial theme so the backend's case-insensitive auto-link kicks in. */
  onUseSuggestion: (title: string) => void;
}

const DISPLAY_LIMIT = 2;

/** Renders 0–2 suggestion cards above the blog list. Returns null when the
 *  API has nothing to show, so the parent can drop it in unconditionally. */
export function TopicSuggestionsPanel({ onUseSuggestion }: Props) {
  const [suggestions, setSuggestions] = useState<TopicSuggestion[] | null>(null);
  // Locally hidden ids (drafted or dismissed). Kept on the client only — the
  // GET will return the same set on next mount, but soft-hiding keeps the UI
  // honest after the user has acted on a card.
  const [hiddenIds, setHiddenIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    api.topicSuggestions.list()
      .then((data) => {
        if (cancelled) return;
        setSuggestions(data.suggestions);
      })
      .catch(() => {
        if (cancelled) return;
        // Treat fetch failure as "no suggestions to show" — the panel is a
        // supplementary nudge, not a blocking surface; surfacing an error
        // banner here would be more disruptive than useful.
        setSuggestions([]);
      });
    return () => { cancelled = true; };
  }, []);

  const onDraft = useCallback((s: TopicSuggestion) => {
    setHiddenIds((prev) => {
      const next = new Set(prev);
      next.add(s.id);
      return next;
    });
    onUseSuggestion(s.title);
  }, [onUseSuggestion]);

  const onDismiss = useCallback(async (id: number) => {
    // Optimistic hide; revert on failure so the user can try again rather
    // than silently losing the card.
    setHiddenIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    try {
      await api.topicSuggestions.dismiss(id);
    } catch {
      setHiddenIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  }, []);

  if (!suggestions) return null;
  const visible = suggestions.filter((s) => !hiddenIds.has(s.id)).slice(0, DISPLAY_LIMIT);
  if (visible.length === 0) return null;

  return (
    <div style={{ marginBottom: 20 }}>
      <div className="view-h">suggested topics</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {visible.map((s) => (
          <SuggestionCard
            key={s.id}
            suggestion={s}
            onDraft={() => onDraft(s)}
            onDismiss={() => void onDismiss(s.id)}
          />
        ))}
      </div>
    </div>
  );
}

const KIND_LABEL: Record<TopicSuggestionKind, string> = {
  daily: 'Today',
  opinion: 'Opinion gap',
};

interface CardProps {
  suggestion: TopicSuggestion;
  onDraft: () => void;
  onDismiss: () => void;
}

function SuggestionCard({ suggestion, onDraft, onDismiss }: CardProps) {
  return (
    <div
      style={{
        position: 'relative',
        padding: 10,
        border: '1px solid var(--line)',
        borderRadius: 6,
        background: 'var(--bg-elev, transparent)',
      }}
    >
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss suggestion"
        title="Dismiss"
        style={{
          position: 'absolute',
          top: 6,
          right: 6,
          width: 20,
          height: 20,
          fontSize: 14,
          lineHeight: '20px',
          color: 'var(--fg-faint)',
          fontFamily: 'var(--mono)',
          cursor: 'pointer',
        }}
      >
        ×
      </button>

      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)' }}>
        {KIND_LABEL[suggestion.kind]}
      </div>

      <div style={{ fontSize: 14, marginTop: 4, fontWeight: 600, paddingRight: 24 }}>
        {suggestion.title}
      </div>

      {suggestion.hook && (
        <div style={{ fontSize: 13, marginTop: 6, color: 'var(--fg-mute)' }}>
          {suggestion.hook}
        </div>
      )}

      {suggestion.source_refs.length > 0 && (
        <div
          style={{
            display: 'flex',
            flexWrap: 'wrap',
            gap: 6,
            marginTop: 8,
            fontFamily: 'var(--mono)',
            fontSize: 11,
            color: 'var(--fg-faint)',
          }}
        >
          {suggestion.source_refs.map((ref, i) => (
            <SourceChip key={`${ref.kind}-${ref.ref}-${i}`} ref_={ref} />
          ))}
        </div>
      )}

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
        <button
          type="button"
          onClick={onDraft}
          style={{
            padding: '4px 10px',
            border: '1px solid var(--accent)',
            borderRadius: 4,
            color: 'var(--accent)',
            fontSize: 12,
            fontFamily: 'var(--mono)',
            cursor: 'pointer',
            background: 'transparent',
          }}
        >
          Draft this →
        </button>
      </div>
    </div>
  );
}

function SourceChip({ ref_ }: { ref_: TopicSourceRef }) {
  const display = `${ref_.kind} ${ref_.ref}`;
  return (
    <span
      title={ref_.label}
      style={{
        padding: '1px 6px',
        border: '1px solid var(--line)',
        borderRadius: 3,
      }}
    >
      {display}
    </span>
  );
}
