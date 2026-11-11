import { useEffect, useRef, useState } from 'react';
import type { Chart, ChartConfiguration } from 'chart.js/auto';
import type { Artifact } from '../types';
import { api } from '../api';
import { MermaidBlock } from './MermaidBlock';

const PELE_RENDERER = 'pele';

interface Props {
  artifact: Artifact;
  onUpdated: (a: Artifact) => void;
  onDeleted: (id: number) => void;
}

export function ArtifactCard({ artifact, onUpdated, onDeleted }: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const deleteTimer = useRef<number | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Close menu on outside click.
  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [menuOpen]);

  // Clear any pending delete-confirm timer on unmount.
  useEffect(() => {
    return () => {
      if (deleteTimer.current !== null) {
        window.clearTimeout(deleteTimer.current);
      }
    };
  }, []);

  const onDeleteClick = async () => {
    if (!confirmingDelete) {
      setConfirmingDelete(true);
      if (deleteTimer.current !== null) window.clearTimeout(deleteTimer.current);
      deleteTimer.current = window.setTimeout(() => {
        setConfirmingDelete(false);
        deleteTimer.current = null;
      }, 3000);
      return;
    }
    if (deleteTimer.current !== null) {
      window.clearTimeout(deleteTimer.current);
      deleteTimer.current = null;
    }
    try {
      await api.deleteArtifact(artifact.id);
      onDeleted(artifact.id);
    } catch {
      // Keep the card — surfacing a toast is out of scope.
      setConfirmingDelete(false);
    }
  };

  return (
    <div className="artifact-card">
      <div className="artifact-head">
        <div className="artifact-head-text">
          <div className="artifact-title">{artifact.title}</div>
          {artifact.description && (
            <div className="artifact-desc">{artifact.description}</div>
          )}
        </div>
        <div className="artifact-menu" ref={menuRef}>
          <button
            type="button"
            className="artifact-menu-btn"
            onClick={() => setMenuOpen((v) => !v)}
            aria-label="Artifact options"
            title="Options"
          >
            ⚙
          </button>
          {menuOpen && (
            <div className="artifact-menu-pop" role="menu">
              <button
                type="button"
                className="artifact-menu-item"
                onClick={() => {
                  setMenuOpen(false);
                  setEditing(true);
                }}
              >
                Edit
              </button>
              <button
                type="button"
                className={
                  'artifact-menu-item danger' + (confirmingDelete ? ' confirming' : '')
                }
                onClick={onDeleteClick}
              >
                {confirmingDelete ? 'Confirm?' : 'Delete'}
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="artifact-body">
        <ArtifactBody artifact={artifact}/>
      </div>

      <div className="artifact-foot">
        <span className="artifact-by">{artifact.created_by ?? 'system'}</span>
        <span className="artifact-dot">·</span>
        <span className="artifact-when">{formatWhen(artifact.created_at)}</span>
      </div>

      {editing && (
        <EditModal
          artifact={artifact}
          onClose={() => setEditing(false)}
          onSaved={(a) => {
            setEditing(false);
            onUpdated(a);
          }}
        />
      )}
    </div>
  );
}

/* ---------- body dispatcher ---------- */

function ArtifactBody({ artifact }: { artifact: Artifact }) {
  switch (artifact.kind) {
    case 'chart':
      return <ChartBody spec={artifact.spec}/>;
    case 'stat':
      return <StatBody spec={artifact.spec}/>;
    case 'timeline':
      return <TimelineBody spec={artifact.spec}/>;
    case 'comparison':
      return <ComparisonBody spec={artifact.spec}/>;
    default:
      return <MalformedNote reason={`Unknown kind: ${String(artifact.kind)}`}/>;
  }
}

/* ---------- chart ---------- */

function ChartBody({ spec }: { spec: Record<string, unknown> }) {
  // Only marked source specs use Pele; legacy stored configs retain their canvas renderer.
  if (!spec || typeof spec !== 'object' || !('renderer' in spec)) {
    return <LegacyChartBody spec={spec}/>;
  }
  if (spec.renderer !== PELE_RENDERER) {
    return <MalformedNote reason="Unsupported chart renderer."/>;
  }
  if (typeof spec.source !== 'string' || !spec.source.trim()) {
    return <MalformedNote reason="Pele chart needs Mermaid source."/>;
  }
  return <MermaidBlock source={spec.source}/>;
}

function LegacyChartBody({ spec }: { spec: Record<string, unknown> }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      setErr('Canvas context unavailable.');
      return;
    }

    // Basic shape check before handing to Chart.js; Chart.js does deeper validation.
    if (!spec || typeof spec !== 'object' || !('type' in spec) || !('data' in spec)) {
      setErr('Chart spec must include `type` and `data`.');
      return;
    }

    // Merge maintainAspectRatio:false so the canvas fills the card height.
    const existingOpts =
      (spec as { options?: Record<string, unknown> }).options ?? {};
    const config: ChartConfiguration = {
      ...(spec as unknown as ChartConfiguration),
      options: {
        ...(existingOpts as Record<string, unknown>),
        maintainAspectRatio: false,
        responsive: true,
      },
    };

    // Chart.js (~200 kB) is only needed by legacy stored charts, so it loads
    // on demand instead of riding in every article's chunk.
    let chart: Chart | null = null;
    let cancelled = false;
    import('chart.js/auto')
      .then(({ default: ChartJs }) => {
        if (cancelled) return;
        chart = new ChartJs(ctx, config);
        setErr(null);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : 'Chart failed to render.');
      });

    return () => {
      cancelled = true;
      if (chart) chart.destroy();
    };
  }, [spec]);

  if (err) return <MalformedNote reason={err}/>;
  return (
    <div className="artifact-chart-wrap">
      <canvas ref={canvasRef} aria-label="chart"/>
    </div>
  );
}

/* ---------- stat ---------- */

function StatBody({ spec }: { spec: Record<string, unknown> }) {
  const value = spec?.value;
  const label = spec?.label;
  if (value === undefined || value === null || typeof label !== 'string') {
    return <MalformedNote reason="Stat needs `value` and `label`."/>;
  }
  const unit = typeof spec.unit === 'string' ? spec.unit : null;
  const delta = typeof spec.delta === 'string' || typeof spec.delta === 'number'
    ? String(spec.delta)
    : null;
  const positive = spec.deltaPositive === true;
  return (
    <div className="artifact-stat">
      <div className="artifact-stat-value">
        <span className="artifact-stat-num">{String(value)}</span>
        {unit && <span className="artifact-stat-unit">{unit}</span>}
      </div>
      <div className="artifact-stat-label">{label}</div>
      {delta !== null && (
        <div className={'artifact-stat-delta ' + (positive ? 'up' : 'down')}>
          {positive ? '▲' : '▼'} {delta}
        </div>
      )}
    </div>
  );
}

/* ---------- timeline ---------- */

interface TimelineEvent {
  date: string;
  label: string;
  detail?: string;
}

function TimelineBody({ spec }: { spec: Record<string, unknown> }) {
  const raw = Array.isArray(spec?.events) ? spec.events : null;
  if (!raw) return <MalformedNote reason="Timeline needs `events: []`."/>;
  const events: TimelineEvent[] = [];
  for (const ev of raw) {
    if (ev && typeof ev === 'object'
      && typeof (ev as Record<string, unknown>).date === 'string'
      && typeof (ev as Record<string, unknown>).label === 'string') {
      const e = ev as Record<string, unknown>;
      events.push({
        date: e.date as string,
        label: e.label as string,
        detail: typeof e.detail === 'string' ? e.detail : undefined,
      });
    }
  }
  if (events.length === 0) {
    return <MalformedNote reason="Timeline has no valid events."/>;
  }
  return (
    <ol className="artifact-timeline">
      {events.map((e, i) => (
        <li key={i} className="artifact-timeline-row">
          <div className="artifact-timeline-date">{e.date}</div>
          <div className="artifact-timeline-body">
            <div className="artifact-timeline-label">{e.label}</div>
            {e.detail && (
              <div className="artifact-timeline-detail">{e.detail}</div>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}

/* ---------- comparison ---------- */

interface ComparisonColumn {
  label: string;
  rows: string[];
}

function ComparisonBody({ spec }: { spec: Record<string, unknown> }) {
  const raw = Array.isArray(spec?.columns) ? spec.columns : null;
  if (!raw) return <MalformedNote reason="Comparison needs `columns: []`."/>;
  const columns: ComparisonColumn[] = [];
  for (const col of raw) {
    if (col && typeof col === 'object'
      && typeof (col as Record<string, unknown>).label === 'string'
      && Array.isArray((col as Record<string, unknown>).rows)) {
      const c = col as Record<string, unknown>;
      const rows = (c.rows as unknown[])
        .filter((r): r is string => typeof r === 'string');
      columns.push({ label: c.label as string, rows });
    }
  }
  if (columns.length === 0) {
    return <MalformedNote reason="Comparison has no valid columns."/>;
  }
  const maxRows = columns.reduce((n, c) => Math.max(n, c.rows.length), 0);
  return (
    <div
      className="artifact-comparison"
      style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }}
    >
      {columns.map((c, i) => (
        <div key={i} className="artifact-comparison-col">
          <div className="artifact-comparison-label">{c.label}</div>
          <ul className="artifact-comparison-rows">
            {Array.from({ length: maxRows }).map((_, r) => (
              <li key={r}>{c.rows[r] ?? ''}</li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

/* ---------- shared helpers ---------- */

function MalformedNote({ reason }: { reason: string }) {
  return <div className="artifact-malformed">Malformed spec — {reason}</div>;
}

function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const now = Date.now();
  const diff = now - d.getTime();
  const min = Math.round(diff / 60_000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 14) return `${day}d ago`;
  return d.toLocaleDateString();
}

/* ---------- edit modal ---------- */

function EditModal({
  artifact,
  onClose,
  onSaved,
}: {
  artifact: Artifact;
  onClose: () => void;
  onSaved: (a: Artifact) => void;
}) {
  const [title, setTitle] = useState(artifact.title);
  const [description, setDescription] = useState(artifact.description ?? '');
  const [specText, setSpecText] = useState(() =>
    JSON.stringify(artifact.spec, null, 2),
  );
  const [parseErr, setParseErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // ESC to close.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const onSave = async () => {
    let parsed: Record<string, unknown>;
    try {
      const v = JSON.parse(specText);
      if (!v || typeof v !== 'object' || Array.isArray(v)) {
        throw new Error('spec must be a JSON object');
      }
      parsed = v as Record<string, unknown>;
    } catch (e) {
      setParseErr(e instanceof Error ? e.message : String(e));
      return;
    }
    setParseErr(null);
    setSaving(true);
    try {
      const next = await api.updateArtifact(artifact.id, {
        title,
        description: description.length ? description : null,
        spec: parsed,
      });
      onSaved(next);
    } catch (e) {
      setParseErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="artifact-modal-scrim" onClick={onClose}>
      <div
        className="artifact-modal"
        role="dialog"
        aria-label="Edit artifact"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="artifact-modal-h">Edit artifact</div>

        <label className="artifact-modal-row">
          <span>Title</span>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>

        <label className="artifact-modal-row">
          <span>Description</span>
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>

        <label className="artifact-modal-row wide">
          <span>Spec (JSON)</span>
          <textarea
            value={specText}
            spellCheck={false}
            onChange={(e) => setSpecText(e.target.value)}
          />
        </label>

        {parseErr && <div className="artifact-modal-err">{parseErr}</div>}

        <div className="artifact-modal-actions">
          <button type="button" className="artifact-btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="artifact-btn primary"
            onClick={onSave}
            disabled={saving}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
